"""Ограниченные HTTP-запросы, кэш JSON и проверенное атомарное хранение файлов."""

from __future__ import annotations

import asyncio
import hashlib
import json as json_module
import math
import os
import random
import tempfile
import time
import warnings
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError


class FetchError(Exception):
    """Ожидаемый сбой, который можно сохранить в статусе кандидата."""

    def __init__(self, status: str, message: str, *, retry_after: float | None = None):
        self.status = status
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)


@dataclass(frozen=True)
class Download:
    sha256: str
    path: Path
    thumbnail_path: Path
    format: str
    width: int
    height: int
    size_bytes: int
    url: str


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: str | None = None):
        self.retry_after = retry_after
        super().__init__(message)


class _MusicBrainzLimiter:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_start: float | None = None

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.last_start is not None:
                delay = 1.01 - (now - self.last_start)
                if delay > 0:
                    await asyncio.sleep(delay)
            self.last_start = time.monotonic()


class _BandcampLimiter:
    """Общий темп метаданных и короткий circuit break после ответа 429."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_start: float | None = None
        self.blocked_until = 0.0
        self.blocked_message = "Bandcamp ограничил частоту запросов."
        self.limit_version = 0

    def _check_cooldown(self, version: int) -> None:
        remaining = self.blocked_until - time.monotonic()
        if remaining > 0 or version != self.limit_version:
            raise FetchError("rate_limited", self.blocked_message, retry_after=max(0.0, remaining))

    async def acquire(self) -> None:
        version = self.limit_version
        async with self.lock:
            self._check_cooldown(version)
            if self.last_start is not None:
                delay = 1.1 - (time.monotonic() - self.last_start)
                if delay > 0:
                    await asyncio.sleep(delay)
            # Пока этот запрос ожидал паузу, предыдущий мог получить 429.
            self._check_cooldown(version)
            self.last_start = time.monotonic()

    def rate_limited(self, url: httpx.URL, retry_after: str | None) -> FetchError:
        delay = _retry_after(retry_after)
        if not math.isfinite(delay) or delay <= 0:
            delay = 60.0
        self.limit_version += 1
        now = time.monotonic()
        self.blocked_until = max(self.blocked_until, now + delay)
        delay = self.blocked_until - now
        self.blocked_message = (
            f"HTTP 429: {url}; пауза Bandcamp {delay:g} с. Текущий проход остановлен."
        )
        return FetchError("rate_limited", self.blocked_message, retry_after=delay)


# Все клиенты в одном приложении/event loop разделяют один ограничитель.
_MB_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_BANDCAMP_LIMITERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_T = TypeVar("_T")
_EXTENSIONS = {
    "JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif",
    "TIFF": "tiff", "BMP": "bmp", "AVIF": "avif", "JPEG2000": "jp2",
}


def _html(data: bytes) -> bool:
    prefix = data[:256].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class Network:
    """Один экземпляр обслуживает метаданные и загрузки текущего сборщика."""

    def __init__(
        self,
        contact: str,
        data_dir: Path,
        concurrency: int = 3,
        timeout: float = 30,
        retries: int = 3,
        max_bytes: int = 30 * 1024**2,
        max_pixels: int = 40_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not contact.strip() or "\n" in contact or "\r" in contact:
            raise ValueError("Укажите действительный контакт или URL проекта для User-Agent.")
        if concurrency < 1 or timeout <= 0 or retries < 0 or max_bytes < 1 or max_pixels < 1:
            raise ValueError("Сетевые ограничения должны быть положительными; retries >= 0.")
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.covers_dir = self.data_dir / "covers"
        self.thumbnail_dir = self.data_dir / "thumbnails"
        self.cache_dir = self.data_dir / "cache"
        for directory in (self.covers_dir, self.thumbnail_dir, self.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.retries = retries
        self.cache_ttl = 24 * 60 * 60
        self._semaphore = asyncio.Semaphore(concurrency)
        self._cache_locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": f"coverpacks/0.1.0 ({contact.strip()})"},
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Network:
        return self

    async def __aexit__(self, *_args) -> None:
        await self._client.aclose()

    async def _retry(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        for attempt in range(self.retries + 1):
            try:
                return await operation()
            except (httpx.TransportError, httpx.DecodingError, _Retryable) as error:
                if attempt >= self.retries:
                    raise FetchError("network_error", f"После {attempt + 1} попыток: {error}") from error
                delay = _retry_after(error.retry_after) if isinstance(error, _Retryable) else 0.0
                # Не игнорируем длинный Retry-After и не держим CLI часами в ожидании.
                if delay > 120:
                    raise FetchError(
                        "network_error", f"{error}; Retry-After={delay:.0f} с: повторите задачу позже."
                    ) from error
                backoff = min(30.0, 0.5 * 2**attempt)
                await asyncio.sleep(max(delay, backoff + random.uniform(0, backoff * 0.25)))
        raise AssertionError("unreachable")

    async def _open(
        self, url: str, params=None, musicbrainz: bool = False,
        *, method: str = "GET", payload: dict | None = None,
    ) -> httpx.Response:
        current = httpx.URL(url, params=params)
        for _ in range(11):
            if current.scheme not in ("https", "http"):
                raise FetchError("http_error", f"Недопустимая схема URL: {current.scheme}")
            if musicbrainz or current.host == "musicbrainz.org" or current.host.endswith(".musicbrainz.org"):
                loop = asyncio.get_running_loop()
                limiter = _MB_LIMITERS.setdefault(loop, _MusicBrainzLimiter())
                await limiter.acquire()
            bandcamp_limiter = None
            if current.host == "bandcamp.com" or current.host.endswith(".bandcamp.com"):
                loop = asyncio.get_running_loop()
                bandcamp_limiter = _BANDCAMP_LIMITERS.setdefault(loop, _BandcampLimiter())
                await bandcamp_limiter.acquire()
            arguments = {"json": payload} if method == "POST" else {}
            response = await self._client.send(self._client.build_request(method, current, **arguments), stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("http_error", "Редирект без Location.")
                    destination = response.url.join(location)
                    if method == "POST" and response.status_code in (307, 308):
                        origin = (current.scheme, current.host, current.port)
                        redirected_origin = (destination.scheme, destination.host, destination.port)
                        if redirected_origin != origin:
                            raise FetchError("http_error", "POST-редирект на другой origin: тело запроса не пересылается.")
                    elif method == "POST":
                        method, payload = "GET", None
                    current = destination
                    await response.aclose()
                    continue
                if response.status_code in (404, 410):
                    raise FetchError("not_found", f"HTTP {response.status_code}: {response.url}")
                if response.status_code in (401, 403, 451):
                    raise FetchError("blocked", f"HTTP {response.status_code}: {response.url}")
                if response.status_code == 429 and bandcamp_limiter is not None:
                    raise bandcamp_limiter.rate_limited(response.url, response.headers.get("retry-after"))
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    raise _Retryable(
                        f"HTTP {response.status_code}: {response.url}", response.headers.get("retry-after")
                    )
                if not 200 <= response.status_code < 300:
                    raise FetchError("http_error", f"HTTP {response.status_code}: {response.url}")
                return response
            except BaseException:
                await response.aclose()
                raise
        raise FetchError("http_error", "Слишком много перенаправлений (более 10).")

    @staticmethod
    def _check_length(response: httpx.Response, limit: int) -> None:
        try:
            length = int(response.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length > limit:
            raise FetchError("too_large", f"Content-Length {length} превышает ограничение {limit} байт.")

    def _cached(self, path: Path, expected_type: type = dict) -> dict | str | None:
        try:
            envelope = json_module.loads(path.read_text(encoding="utf-8"))
            age = time.time() - float(envelope["created_at"])
            if 0 <= age < self.cache_ttl and isinstance(envelope["data"], expected_type):
                return envelope["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def _write_cache(path: Path, data: dict | str) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".part", delete=False) as file:
                temporary = Path(file.name)
                json_module.dump({"created_at": time.time(), "data": data}, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def json(self, url: str, params=None, musicbrainz: bool = False) -> dict:
        return await self._document(url, params=params, musicbrainz=musicbrainz)

    async def post_json(self, url: str, payload: dict, *, fresh: bool = False) -> dict:
        """POST только для чтения; fresh обходит чтение кэша, обновляя его при успехе."""
        if not isinstance(payload, dict):
            raise ValueError("Тело post_json должно быть JSON-объектом.")
        return await self._document(url, method="POST", payload=payload, fresh=fresh)

    async def text(self, url: str) -> str:
        """Ограниченный GET текста/HTML с теми же повторами и кэшем на 24 часа."""
        return await self._document(url, as_text=True)

    async def _document(
        self, url: str, params=None, musicbrainz: bool = False,
        *, method: str = "GET", payload: dict | None = None, as_text: bool = False,
        fresh: bool = False,
    ) -> dict | str:
        query = sorted(httpx.QueryParams(params).multi_items()) if params is not None else []
        # Сохраняем совместимость с существующим GET JSON-кэшем. Другие виды
        # запросов имеют отдельное пространство ключей, включая тело POST.
        identity = [url, query] if method == "GET" and not as_text else [method, "text" if as_text else "json", url, query, payload]
        key = hashlib.sha256(json_module.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json"
        async with self._cache_locks.setdefault(key, asyncio.Lock()):
            if not fresh:
                cached = self._cached(path, str if as_text else dict)
                if cached is not None:
                    return cached

            async def fetch() -> dict | str:
                async with self._semaphore:
                    response = await self._open(url, params=params, musicbrainz=musicbrainz, method=method, payload=payload)
                    try:
                        limit = min(self.max_bytes, 8 * 1024**2)
                        self._check_length(response, limit)
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > limit:
                                raise FetchError("too_large", f"Ответ превышает ограничение {limit} байт.")
                            body.extend(chunk)
                        if as_text:
                            return body.decode(response.encoding or "utf-8", errors="replace")
                        if _html(body):
                            raise FetchError("invalid_json", "Сервер вернул HTML вместо JSON.")
                        try:
                            result = json_module.loads(body)
                        except (ValueError, UnicodeError) as error:
                            raise FetchError("invalid_json", "Ответ не является корректным JSON.") from error
                        if not isinstance(result, dict):
                            raise FetchError("invalid_json", "Ожидался JSON-объект.")
                        return result
                    finally:
                        await response.aclose()

            result = await self._retry(fetch)
            self._write_cache(path, result)
            return result

    def _finish_download(self, temporary: Path, digest: str, size: int, url: str) -> Download:
        thumbnail_temporary: Path | None = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(temporary) as source:
                    image_format = source.format
                    width, height = source.size
                    if width * height > self.max_pixels:
                        raise FetchError("too_many_pixels", f"Изображение {width}×{height} превышает {self.max_pixels} пикселей.")
                    if image_format not in _EXTENSIONS:
                        raise FetchError("invalid_image", f"Неподдерживаемый формат изображения: {image_format}.")
                    source.verify()
                with Image.open(temporary) as source:
                    source.load()  # verify() не обнаруживает все варианты усечённого JPEG.
                    preview = ImageOps.exif_transpose(source)
                    preview.thumbnail((320, 320), Image.Resampling.LANCZOS)
                    if preview.mode in ("RGBA", "LA") or "transparency" in preview.info:
                        rgba = preview.convert("RGBA")
                        preview = Image.new("RGB", rgba.size, "white")
                        preview.paste(rgba, mask=rgba.getchannel("A"))
                    else:
                        preview = preview.convert("RGB")
                    with tempfile.NamedTemporaryFile(dir=self.thumbnail_dir, suffix=".part", delete=False) as file:
                        thumbnail_temporary = Path(file.name)
                        preview.save(file, format="JPEG", quality=85, optimize=True)
                        file.flush()
                        os.fsync(file.fileno())
            destination = self.covers_dir / f"{digest}.{_EXTENSIONS[image_format]}"
            thumbnail_path = self.thumbnail_dir / f"{digest}.jpg"
            if temporary != destination:
                os.replace(temporary, destination)
            os.replace(thumbnail_temporary, thumbnail_path)
            return Download(digest, destination, thumbnail_path, image_format, width, height, size, url)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise FetchError("too_many_pixels", str(error)) from error
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
            raise FetchError("invalid_image", f"Изображение не прошло проверку: {error}") from error
        finally:
            if thumbnail_temporary is not None:
                thumbnail_temporary.unlink(missing_ok=True)

    def verify_existing(self, metadata: dict) -> Download | None:
        """Проверить сохранённый blob и восстановить превью без сетевого запроса.

        Выбор совпадающего source ID И варианта качества делает вызывающий код.
        Отсутствующие/повреждённые файлы возвращают None; исходник не изменяется.
        """
        try:
            digest = metadata["sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                return None
            path = (self.data_dir / metadata["path"]).resolve()
            if path.parent != self.covers_dir or not path.is_file():
                return None
            size = path.stat().st_size
            if size > self.max_bytes or size < 1:
                return None
            with path.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                    return None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as source:
                    extension = _EXTENSIONS.get(source.format)
                    if not extension or path.name != f"{digest}.{extension}":
                        return None
            return self._finish_download(path, digest, size, metadata.get("downloaded_url") or metadata.get("requested_url") or "")
        except (KeyError, TypeError, ValueError, OSError, FetchError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            return None

    async def download(self, url: str) -> Download:
        async def fetch() -> Download:
            async with self._semaphore:
                temporary: Path | None = None
                response = await self._open(url)
                try:
                    self._check_length(response, self.max_bytes)
                    digest = hashlib.sha256()
                    total = 0
                    prefix = bytearray()
                    with tempfile.NamedTemporaryFile(dir=self.covers_dir, suffix=".part", delete=False) as file:
                        temporary = Path(file.name)
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise FetchError("too_large", f"Файл превышает ограничение {self.max_bytes} байт.")
                            if len(prefix) < 256:
                                prefix.extend(chunk[:256 - len(prefix)])
                            if _html(prefix):
                                raise FetchError("invalid_image", "Сервер вернул HTML вместо изображения.")
                            file.write(chunk)
                            digest.update(chunk)
                        file.flush()
                        os.fsync(file.fileno())
                    return self._finish_download(temporary, digest.hexdigest(), total, str(response.url))
                finally:
                    await response.aclose()
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

        return await self._retry(fetch)

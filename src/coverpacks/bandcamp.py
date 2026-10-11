"""Read-only Bandcamp vinyl discovery and album artwork collection.

The discover endpoint is the site's observed internal endpoint, not a public API
contract. Page shapes and artwork identity are checked before advancing a cursor
or downloading anything. Artwork is album art for a listed vinyl product; it is
not asserted to be a scan of a particular physical edition.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from ipaddress import ip_address
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

from .api import ApiDataError
from .network import Download, FetchError

DISCOVERY_URL = "https://bandcamp.com/discover/all/vinyl"
DISCOVERY_API = "https://bandcamp.com/api/discover/1/discover_web"
PAGE_SIZE = 60
QUALITY = "bandcamp-full"


@dataclass(frozen=True)
class BandcampPage:
    items: list[dict]
    next_cursor: str | None
    total: int
    exhausted_reason: str | None = None


def _positive_id(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApiDataError(f"Bandcamp: некорректный {name}")
    return value


def album_url(value):
    """Accept public album URLs from Discover, including artists' custom domains."""
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        host = parsed.hostname if parsed else None
        labels = host.split(".") if host else []
        public_name = (len(labels) >= 2 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            and bool(re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]+", labels[-1]))
            and labels[-1] not in {"localhost", "local", "internal", "home", "lan", "test", "invalid", "example", "onion"})
        try:
            ip_address(host or "")
            public_name = False
        except ValueError:
            pass
        if (parsed and parsed.scheme == "https" and public_name
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443)
                and re.fullmatch(r"/album/[^/]+/?", parsed.path)
                and "\\" not in value and not any(c.isspace() or ord(c) < 32 for c in value)):
            return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
    except ValueError:
        pass
    raise ApiDataError("Bandcamp: отсутствует безопасный URL страницы альбома")


def _art_url(value, image_id):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (not parsed or parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".bcbits.com")
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment
                or any(ord(c) < 32 for c in value)):
            return None
        match = re.fullmatch(r"/img/a(\d+)_[A-Za-z0-9_]+\.(?:jpg|jpeg|png|webp|avif)", parsed.path)
        return value if match and int(match[1]) == image_id else None
    except ValueError:
        return None


class _ArtworkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.found = False
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif attrs.get("id") == "tralbumArt":
                self.found = True
                self.depth = 1
        if not self.depth:
            return
        if tag == "a" and "popupImage" in (attrs.get("class") or "").split():
            self.links.append(attrs.get("href"))
        elif tag == "img":
            self.images.append(attrs.get("src"))

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1


def parse_artwork(html, image_id):
    """Use the actual full-size anchor, never manufacture a CDN size suffix."""
    image_id = _positive_id(image_id, "artwork image ID")
    parser = _ArtworkParser()
    parser.feed(html)
    parser.close()
    if not parser.found:
        raise FetchError("no_front", "Bandcamp: на странице не найден блок обложки tralbumArt")
    matches = {url for value in parser.links if (url := _art_url(value, image_id))}
    if len(matches) != 1 or not any(_art_url(value, image_id) for value in parser.images):
        raise ApiDataError("Bandcamp: большая обложка страницы не соответствует artwork ID из Discover")
    return matches.pop()


def _sanitize(item):
    if not isinstance(item, dict):
        raise ApiDataError("Bandcamp: некорректная запись Discover")
    item_id = _positive_id(item.get("item_id"), "item_id")
    band_id = _positive_id(item.get("band_id"), "band_id")
    item_type = item.get("item_type")
    if not isinstance(item_type, str) or not re.fullmatch(r"[a-z]{1,20}", item_type):
        raise ApiDataError("Bandcamp: некорректный item_type")
    kind = "album" if item_type == "a" else f"item-{item_type}"
    result = {key: item.get(key) for key in (
        "item_id", "item_type", "result_type", "title", "item_url", "band_id",
        "album_artist", "band_name", "release_date",
    )}
    result["id"] = f"bandcamp:{kind}:{band_id}:{item_id}"
    primary = item.get("primary_image")
    result["primary_image"] = ({key: primary.get(key) for key in ("image_id", "is_art")}
                               if isinstance(primary, dict) else None)
    packages = item.get("package_info")
    if not isinstance(packages, list) or any(not isinstance(p, dict) for p in packages):
        raise ApiDataError("Bandcamp: некорректный package_info")
    result["package_info"] = [
        {key: package.get(key) for key in ("package_id", "format", "title", "is_preorder", "image_id", "type_id")}
        for package in packages
    ]
    return result


class BandcampAPI:
    def __init__(self, network):
        self.network = network

    async def discover(self, cursor="*", size=PAGE_SIZE, *, fresh=False):
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError("Некорректный курсор Bandcamp")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= PAGE_SIZE:
            raise ValueError(f"Размер страницы Bandcamp должен быть 1–{PAGE_SIZE}")
        payload = {
            "category_id": 2, "tag_norm_names": [], "geoname_id": 0,
            "slice": "top", "time_facet_id": None, "cursor": cursor, "size": size,
            "include_result_types": ["a", "s"], "followed_bands": False,
        }
        if fresh:
            data = await self.network.post_json(DISCOVERY_API, payload, fresh=True)
        else:
            data = await self.network.post_json(DISCOVERY_API, payload)
        if not isinstance(data, dict):
            raise ApiDataError("Bandcamp: Discover не вернул объект JSON")
        raw = data.get("results")
        total, batch = data.get("result_count"), data.get("batch_result_count")
        if (not isinstance(raw, list) or len(raw) > size
                or isinstance(total, bool) or not isinstance(total, int) or total < 0
                or isinstance(batch, bool) or not isinstance(batch, int) or batch != len(raw)):
            raise ApiDataError("Bandcamp: некорректная страница или счётчик Discover")
        items = [_sanitize(item) for item in raw]
        next_cursor = data.get("cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096):
            raise ApiDataError("Bandcamp: некорректный следующий курсор")
        if items and next_cursor == cursor:
            raise ApiDataError("Bandcamp повторил текущий курсор; страница не сохранена")
        if not items:
            return BandcampPage(items, None, total, "Bandcamp вернул пустую страницу; весь каталог не проверен")
        if not next_cursor:
            return BandcampPage(items, None, total, "Исчерпана текущая выдача Bandcamp Discover, а не весь каталог")
        return BandcampPage(items, next_cursor, total)

    async def artwork(self, item):
        url = album_url(item.get("item_url"))
        primary = item.get("primary_image") or {}
        image_id = _positive_id(primary.get("image_id"), "artwork image ID")
        return parse_artwork(await self.network.text(url), image_id)


class BandcampCollector:
    def __init__(self, state, api, network, concurrency=3):
        if concurrency < 1:
            raise ValueError("concurrency должен быть положительным")
        self.state, self.api, self.network = state, api, network
        self.concurrency = concurrency
        self._source_locks = {}
        self._downloads = {}
        self.rate_limited_until = None

    def _rate_limit(self, retry_after=None):
        self.rate_limited_until = time.time() + (retry_after or 60)
        return self.rate_limited_until

    async def download_source(self, entity_id, image_id, url):
        key = ("bandcamp", entity_id, str(image_id), QUALITY)
        async with self._source_locks.setdefault(key, asyncio.Lock()):
            if key in self._downloads:
                return self._downloads[key]
            cached = self.state.cached_external_source(*key)
            downloaded = None
            if cached and hasattr(self.network, "verify_existing"):
                downloaded = self.network.verify_existing(cached)
            elif (cached and (self.state.root / cached["thumbnail_path"]).is_file()
                  and cached["size_bytes"] <= self.network.max_bytes
                  and cached["width"] * cached["height"] <= self.network.max_pixels):
                downloaded = Download(cached["sha256"], self.state.root / cached["path"],
                    self.state.root / cached["thumbnail_path"], cached["format"],
                    cached["width"], cached["height"], cached["size_bytes"], cached["downloaded_url"])
            if downloaded is None:
                downloaded = await self.network.download(url)
            self._downloads[key] = downloaded
            return downloaded

    def _download_info(self, downloaded):
        info = asdict(downloaded)
        info["path"] = str(downloaded.path.relative_to(self.state.root))
        info["thumbnail_path"] = str(downloaded.thumbnail_path.relative_to(self.state.root))
        info["downloaded_url"] = info.pop("url")
        return info

    async def process(self, entry, config):
        try:
            if entry.get("result"):
                previous = json.loads(entry["result"])
                downloaded = await self.download_source(previous["source_entity_id"],
                    previous["source_image_id"], previous["requested_url"])
                if downloaded.sha256 != previous["sha256"]:
                    previous["needs_review"] = True
                    previous["review_reasons"] = previous.get("review_reasons", []) + ["Байты источника изменились при восстановлении"]
                    previous["previous_sha256"] = previous["sha256"]
                return {**previous, **self._download_info(downloaded)}
            item = json.loads(entry["metadata"])
            if item.get("item_type") != "a":
                return {"error_status": "not_album", "error": "Bandcamp: запись Discover не является альбомом"}
            vinyl = [package for package in item["package_info"] if package.get("format") == "vinyl"]
            if not vinyl:
                return {"error_status": "no_vinyl", "error": "Bandcamp: у альбома нет товара с format=vinyl"}
            primary = item.get("primary_image") or {}
            if primary.get("is_art") is not True:
                return {"error_status": "no_front", "error": "Bandcamp: Discover не указал album artwork"}
            image_id = str(_positive_id(primary.get("image_id"), "artwork image ID"))
            source_url = album_url(item.get("item_url"))
            title, artist = item.get("title"), item.get("album_artist") or item.get("band_name")
            if not isinstance(title, str) or not title.strip() or not isinstance(artist, str) or not artist.strip():
                raise ApiDataError("Bandcamp: отсутствует название альбома или исполнитель")
            for package in vinyl:
                _positive_id(package.get("package_id"), "vinyl package_id")
            url = await self.api.artwork(item)
            downloaded = await self.download_source(item["id"], image_id, url)
            review = []
            if min(downloaded.width, downloaded.height) < 600:
                review.append("Короткая сторона меньше 600 px")
            if max(downloaded.width, downloaded.height) / min(downloaded.width, downloaded.height) > 1.15:
                review.append("Соотношение сторон отличается от квадрата более чем на 15%")
            return {**self._download_info(downloaded), "source": "bandcamp",
                "source_entity_id": item["id"], "source_image_id": image_id,
                "bandcamp_item_id": item["item_id"], "bandcamp_band_id": item["band_id"],
                "vinyl_packages": vinyl, "source_url": source_url, "discovery_url": DISCOVERY_URL,
                "image_url": url, "requested_url": url, "front": True, "approved": None,
                "artwork_type": "album_art", "requested_quality": QUALITY, "actual_quality": QUALITY,
                "selection_reason": "Bandcamp Discover vinyl; большая обложка альбома из tralbumArt с совпадающим artwork ID; это album art, не подтверждённый скан конкретного винилового издания",
                "title": title, "artist": artist, "date": item.get("release_date"),
                "release_date": item.get("release_date"), "media_formats": ["Vinyl"],
                "needs_review": bool(review), "review_reasons": review}
        except FetchError as exc:
            result = {"error_status": exc.status, "error": str(exc)}
            if exc.status == "rate_limited" and exc.retry_after is not None:
                result["retry_after"] = exc.retry_after
            return result
        except (ApiDataError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return {"error_status": "api_error", "error": f"Bandcamp: {exc}"}

    async def refresh_head(self, task_id, *, max_pages=4):
        """Read a bounded fresh window before draining the existing queue.

        Ranking can reorder known and new albums, so known IDs never stop a
        pass. Each page commits separately; a failed pass can restart at '*'
        without changing old entries or the normal discovery checkpoint.
        """
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("max_pages должен быть положительным целым числом")
        if self.state.task(task_id)["config"].get("source") != "bandcamp":
            raise ValueError("Обновление Discover доступно только для задачи Bandcamp")
        self.rate_limited_until = None
        report = {"pages": 0, "examined": 0, "added": 0, "known": 0, "stopped": "page_limit"}
        cursor, seen = "*", set()
        for _ in range(max_pages):
            seen.add(cursor)
            try:
                page = await self.api.discover(cursor, PAGE_SIZE, fresh=True)
            except FetchError as exc:
                if exc.status != "rate_limited":
                    raise
                report.update(stopped="rate_limited", error=str(exc),
                              not_before=self._rate_limit(exc.retry_after))
                if exc.retry_after is not None:
                    report["retry_after"] = exc.retry_after
                break
            if page.next_cursor in seen:
                raise ApiDataError("Bandcamp вернул цикл курсоров при обновлении; страница не сохранена")
            added = self.state.stage_bandcamp_head_page(task_id, page.items)
            report["pages"] += 1
            report["examined"] += len(page.items)
            report["added"] += added
            report["known"] += len(page.items) - added
            if not page.items or page.next_cursor is None:
                report["stopped"] = "end_of_results"
                break
            cursor = page.next_cursor
        return report

    async def run(self, task_id, *, discover=True, archived_hashes=()):
        self.rate_limited_until = None
        self.state.recover(task_id)
        repaired = self.state.audit_files(task_id, archived_hashes=archived_hashes)
        if repaired:
            print(f"Нужно восстановить записей: {repaired}")
        while True:
            task = self.state.task(task_id)
            count = self.state.count(task_id)
            repairs = self.state.repairs(task_id, self.concurrency)
            if count >= task["target"] and not repairs:
                reason = "Достигнут целевой размер коллекции"
                break
            batch = repairs[:self.concurrency] if repairs else self.state.queue(task_id, min(self.concurrency, task["target"] - count))
            if not batch:
                if not discover:
                    reason = "Очередь повторов обработана; новые кандидаты не запрашивались"
                    break
                if task["exhausted"]:
                    reason = task["exhausted"]
                    break
                remaining = task["max_candidates"] - task["discovered"]
                if remaining <= 0:
                    reason = "Достигнут лимит просматриваемых кандидатов; можно увеличить --max-candidates"
                    break
                try:
                    cursor = task["cursor"]
                    if cursor in self.state.bandcamp_cursors(task_id):
                        raise ApiDataError("Bandcamp повторил уже сохранённый курсор")
                    page = await self.api.discover(cursor, min(PAGE_SIZE, remaining))
                    if page.next_cursor == cursor or page.next_cursor in self.state.bandcamp_cursors(task_id):
                        raise ApiDataError("Bandcamp вернул цикл курсоров; страница не сохранена")
                    self.state.stage_bandcamp_page(task_id, page.items, page.next_cursor,
                        page.exhausted_reason if page.next_cursor is None else None)
                except (FetchError, ApiDataError) as exc:
                    reason = f"Поиск остановлен: {getattr(exc, 'status', 'api_error')}: {exc}. Checkpoint сохранён"
                    if isinstance(exc, FetchError) and exc.status == "rate_limited":
                        self._rate_limit(exc.retry_after)
                    if getattr(exc, "retry_after", None) is not None:
                        reason += f"; Retry-After={exc.retry_after:g} с"
                    break
                continue
            for entry in batch:
                self.state.begin(entry["seq"])
            results = await asyncio.gather(*(self.process(entry, task["config"]) for entry in batch))
            for entry, result in zip(batch, results, strict=True):
                if "error_status" in result:
                    status = "pending" if result["error_status"] == "rate_limited" else result["error_status"]
                    self.state.failed(entry["seq"], status, result["error"])
                else:
                    self.state.success(entry["seq"], result)
                print(f"seq={entry['seq']} {result.get('error_status', 'saved')} · коллекция {self.state.count(task_id)}/{task['target']}", flush=True)
            limited = [result for result in results if result.get("error_status") == "rate_limited"]
            if limited:
                self._rate_limit(max(result.get("retry_after") or 60 for result in limited))
                reason = f"Сбор остановлен: rate_limited: {limited[0]['error']}. Пачка сохранена, затронутые записи оставлены в очереди"
                retry_after = [result["retry_after"] for result in limited if result.get("retry_after") is not None]
                if retry_after:
                    reason += f"; Retry-After={max(retry_after):g} с"
                break
        self.state.set_task(task_id, stop_reason=reason)
        return reason

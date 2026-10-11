"""Crash-aware, sequential Telegram publication from frozen local manifests.

The ledger contains publication intent before each externally visible write.
Telegram access is injected; this module never logs in or retries by sleeping.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from telethon import errors

from .state import collection_lock


class PublicationError(ValueError):
    """A publication invariant failed before the next external write."""


class ReconciliationRequired(PublicationError):
    """An ambiguous or externally changed resource needs explicit reconciliation."""


class DeferredPublication(PublicationError):
    def __init__(self, wait_seconds, not_before, operation):
        self.wait_seconds = max(1, math.ceil(wait_seconds))
        self.not_before = not_before
        self.operation = operation
        super().__init__(f"Telegram попросил подождать {self.wait_seconds} с. Операция {operation} сохранена; повторите publish после паузы.")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _label(text, maximum=90):
    text = " ".join(text.split())
    if _units(text) <= maximum:
        return text
    result, length = [], 0
    for char in text:
        width = _units(char)
        if length + width > maximum - 1:
            break
        result.append(char)
        length += width
    return "".join(result).rstrip() + "…"


def _search_keywords(item):
    """Fit artist and album into Telegram's small per-emoji search budget.

    Keep full phrases when possible. For long names, alternate distinctive
    words from the title and artist; complete names remain in the photo caption.
    """
    phrases = []
    for value in (item.get("title"), item.get("artist")):
        phrase = " ".join((value or "").replace(",", " ").split())
        if phrase and phrase.casefold() not in {p.casefold() for p in phrases}:
            phrases.append(phrase)
    if _units(",".join(phrases)) <= 64:
        return ",".join(phrases)
    stop = {"the", "a", "an", "and", "of", "to", "like", "-", "—", "&"}
    words = [[word for word in phrase.split() if word.casefold() not in stop] for phrase in phrases]
    candidates = []
    for index in range(max((len(group) for group in words), default=0)):
        candidates.extend(group[index] for group in words if index < len(group))
    chosen, seen = [], set()
    for word in candidates:
        if word.casefold() in seen:
            continue
        if _units(",".join(chosen + [word])) <= 64 and len(chosen) < 20:
            chosen.append(word)
            seen.add(word.casefold())
    if not chosen and phrases:
        # An unusually long single token still gets a useful prefix; no
        # shortened name is substituted into the visible album catalogue.
        chosen = [_label(phrases[0], 64).rstrip("…")]
    return ",".join(chosen)


def _https(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)


def _validate_file_reference(path, digest):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise PublicationError("Manifest должен содержать абсолютные пути к файлам.")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PublicationError("В manifest отсутствует корректный SHA-256.")


def _read_checked(path, digest, maximum):
    _validate_file_reference(path, digest)
    try:
        with Path(path).open("rb") as handle:
            content = handle.read(maximum + 1)
    except OSError as error:
        raise PublicationError(f"Не удалось прочитать файл публикации: {path}") from error
    if len(content) > maximum or _hash(content) != digest:
        raise PublicationError(f"Размер или SHA-256 файла изменился: {path}")
    return content


def _validate_manifest(path, *, archived_packs=None):
    try:
        content = Path(path).read_bytes()
        document = json.loads(content)
    except (OSError, ValueError) as error:
        raise PublicationError("Не удалось прочитать manifest emoji.") from error
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or not isinstance(document.get("brand"), str) or not 1 <= len(document["brand"]) <= 40
            or not isinstance(document.get("prefix"), str)
            or type(document.get("task_id")) is not int or document["task_id"] <= 0
            or type(document.get("pack_size")) is not int or not 1 <= document["pack_size"] <= 200
            or not isinstance(document.get("packs"), list) or not document["packs"]):
        raise PublicationError("Неподдерживаемый или пустой manifest emoji.")
    match = re.fullmatch(r"manifest-([0-9a-f]{16})\.json", Path(path).name)
    if match and not _hash(content).startswith(match[1]):
        raise PublicationError("Содержимое immutable manifest изменено.")
    seen_sources, seen_names = set(), set()
    for index, pack in enumerate(document["packs"], 1):
        try:
            if (type(pack["number"]) is not int or pack["number"] != index
                    or not isinstance(pack["title"], str) or not 1 <= len(pack["title"]) <= 64
                    or not isinstance(pack["short_name"], str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", pack["short_name"])
                    or "__" in pack["short_name"] or pack["short_name"].endswith("_")
                    or pack["short_name"] in seen_names
                    or not isinstance(pack["items"], list) or not 1 <= len(pack["items"]) <= 200
                    or not isinstance(pack.get("emoji"), str) or not pack["emoji"]):
                raise PublicationError("Неверные параметры или порядок паков в manifest.")
            seen_names.add(pack["short_name"])
            archived = (archived_packs or {}).get(pack["short_name"]) == _hash(_json(pack).encode())
            for position, item in enumerate(pack["items"], 1):
                if (item["position"] != position or item["width"] != 100 or item["height"] != 100
                        or item["source_sha256"] in seen_sources or not _https(item["source_url"])):
                    raise PublicationError("Неверный порядок, повтор обложки или ссылка источника в manifest.")
                seen_sources.add(item["source_sha256"])
                if archived:
                    # Only an exact payload from the trusted published ledger
                    # may omit its local art; metadata still passes validation.
                    for extension in ("source", "webp", "png"):
                        _validate_file_reference(item[f"{extension}_path"], item[f"{extension}_sha256"])
                    continue
                original = _read_checked(item["source_path"], item["source_sha256"], 30 * 1024 * 1024)
                with Image.open(io.BytesIO(original)) as image:
                    if image.width * image.height > 40_000_000 or getattr(image, "n_frames", 1) != 1:
                        raise PublicationError("Оригинал должен быть статичным изображением допустимого размера.")
                    image.verify()
                pixels = []
                for extension in ("webp", "png"):
                    derived = _read_checked(item[f"{extension}_path"], item[f"{extension}_sha256"], 512 * 1024)
                    with Image.open(io.BytesIO(derived)) as image:
                        if image.size != (100, 100) or image.format != extension.upper() or getattr(image, "n_frames", 1) != 1:
                            raise PublicationError("Ожидалось одно статичное emoji 100×100 PNG/WebP.")
                        image.load()
                        pixels.append(image.convert("RGBA").tobytes())
                if pixels[0] != pixels[1]:
                    raise PublicationError("PNG и WebP в manifest содержат разные изображения.")
        except (KeyError, TypeError, OSError, SyntaxError, Image.DecompressionBombError) as error:
            raise PublicationError("Повреждены данные или изображения в manifest.") from error
    if document.get("item_count") != len(seen_sources):
        raise PublicationError("Число emoji не совпадает с manifest.")
    return document, _hash(content)


def _validate_config(config):
    try:
        result = {key: config[key] for key in ("title", "about", "username")}
        for key, default in (("photo_previews", False), ("welcome_enabled", True)):
            value = config.get(key, default)
            if type(value) is not bool:
                raise PublicationError(f"Настройка {key} должна быть true или false.")
            result[key] = value
        if (not isinstance(result["title"], str) or not 1 <= len(result["title"]) <= 128
                or not isinstance(result["about"], str) or len(result["about"]) > 255
                or not isinstance(result["username"], str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", result["username"])):
            raise PublicationError("Некорректные название, описание или username канала.")
        avatar = config.get("avatar")
        if avatar:
            path = Path(avatar).resolve()
            content = path.read_bytes()
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            result.update(avatar=str(path), avatar_sha256=_hash(content))
        else:
            result.update(avatar=None, avatar_sha256=None)
        return result
    except (KeyError, TypeError, OSError, ValueError) as error:
        if isinstance(error, PublicationError):
            raise
        raise PublicationError("Не удалось проверить настройки канала или его аватар.") from error


class PublicationState:
    def __init__(self, root: Path):
        root = Path(root).resolve()
        self.path = root if root.suffix in {".sqlite3", ".db"} else root / "publish.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads (sha256 TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS packs (
                number INTEGER PRIMARY KEY, short_name TEXT UNIQUE NOT NULL,
                pack_hash TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned', documents TEXT, remote_set TEXT,
                thumbnail_done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS posts (
                key TEXT PRIMARY KEY, pack_name TEXT REFERENCES packs(short_name),
                part INTEGER NOT NULL, random_id INTEGER NOT NULL UNIQUE,
                body TEXT NOT NULL, documents TEXT NOT NULL, links TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned', message_id INTEGER, url TEXT
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        for column in ("photo_path", "photo_sha256"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE posts ADD COLUMN {column} TEXT")
        if "formatting" not in columns:
            self.db.execute("ALTER TABLE posts ADD COLUMN formatting TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()
        for file in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if file.exists():
                file.chmod(0o600)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))

    def report(self):
        channel = self.get("channel") or {}
        username = channel.get("username")
        welcome = self.db.execute("SELECT message_id FROM posts WHERE key='welcome' AND status='sent'").fetchone()
        packs = []
        for pack in self.db.execute("SELECT * FROM packs ORDER BY number"):
            posts = self.db.execute("SELECT message_id,url FROM posts WHERE pack_name=? AND status='sent' ORDER BY part", (pack["short_name"],)).fetchall()
            packs.append({"short_name": pack["short_name"], "status": pack["status"],
                          "message_ids": [post["message_id"] for post in posts],
                          "links": [post["url"] for post in posts]})
        deferred = self.get("deferred") or {}
        return {"channel_url": f"https://t.me/{username}" if username else None,
                "account_id": self.get("account_id"), "welcome_message_id": welcome[0] if welcome else None,
                "packs": packs, "not_before": deferred.get("not_before")}


class Publisher:
    def __init__(self, state: PublicationState, transport, *, now=time.time, checkpoint=None):
        self.state = state
        self.transport = transport
        self.now = now
        self.checkpoint = checkpoint

    async def _checkpoint(self):
        if self.checkpoint is not None:
            await self.checkpoint()

    async def _call(self, operation, method, *args, **kwargs):
        self.state.put("last_operation", operation)
        if operation in {"create_channel", "configure_channel"} or operation.startswith(("create_set:", "send_post:")):
            # The callback must durably save the existing local intent before
            # the request can become visible outside this process.
            await self._checkpoint()
        try:
            return await method(*args, **kwargs)
        except errors.FloodWaitError as error:
            wait = max(1, error.seconds)
            not_before = self.now() + wait
            self.state.put("deferred", {"not_before": not_before, "operation": operation})
            raise DeferredPublication(wait, not_before, operation) from error
        except Exception as error:
            self.state.put("last_error", {"operation": operation, "type": type(error).__name__})
            raise

    async def run(self, manifest_path: Path, config: dict):
        with collection_lock(self.state.path.parent):
            document, manifest_hash = _validate_manifest(manifest_path)
            checked_config = _validate_config(config)
            self._freeze_plan(document, checked_config, manifest_hash)
            deferred = self.state.get("deferred")
            if deferred and deferred["not_before"] > self.now():
                raise DeferredPublication(deferred["not_before"] - self.now(), deferred["not_before"], deferred["operation"])
            if deferred:
                self.state.put("deferred", None)
            account, channel, capabilities = await self._publication_context(document, checked_config)
            for pack in document["packs"]:
                await self._pack(channel, pack, bool(account.get("premium")), document["brand"],
                                 capabilities["max_caption_length"])
            return self.state.report()

    async def _publication_context(self, document, config):
        account = await self._call("account", self.transport.me)
        account_id = account.get("id")
        if type(account_id) is not int or account_id <= 0:
            raise PublicationError("Telegram не вернул корректный ID личного аккаунта.")
        previous_account = self.state.get("account_id")
        if previous_account is not None and previous_account != account_id:
            raise PublicationError("Этот журнал публикаций привязан к другому Telegram-аккаунту.")
        self.state.put("account_id", account_id)
        channel = await self._channel(config)
        capabilities = {"max_caption_length": 1024}
        if config["photo_previews"]:
            capabilities = await self._call("capabilities", self.transport.capabilities)
            self.state.put("account_capabilities", capabilities)
        if config["welcome_enabled"]:
            self._plan_welcome(document["brand"])
            await self._post(channel, "welcome")
        return account, channel, capabilities

    def _daily_result(self, status, pack=None, **extra):
        return {"status": status, "pack": pack,
                "next_due_at": self.state.get("daily_next_due_at"),
                "report": self.state.report(), **extra}

    def _complete_daily(self, active):
        completed_at = self.now()
        interval = active["interval_seconds"]
        if "scheduled_for" in active:
            anchor = active["scheduled_for"]
            # Skip missed slots instead of shifting the schedule by processing
            # time or immediately publishing an accumulated queue.
            periods = max(1, math.floor((completed_at - anchor) / interval) + 1)
            next_due_at = anchor + periods * interval
        else:
            # Preserve recovery semantics for intents saved before slot anchoring.
            next_due_at = completed_at + interval
        with self.state.db:
            self.state.db.executemany(
                "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, _json(value)) for key, value in (
                    ("daily_last_completed_at", completed_at),
                    ("daily_next_due_at", next_due_at),
                    ("daily_active_pack", None),
                )])
        return self._daily_result("published", active["short_name"])

    async def publish_next(self, manifest_path: Path, config: dict, *, interval_seconds=86400):
        """Publish at most one queued pack, resuming its durable intent first.

        New intents retain the due-time anchor across retries. Completion moves
        to the first future slot on that interval grid, skipping missed slots.
        Legacy intents without an anchor keep their completion-based interval.
        A finished intent only completes local bookkeeping, never advances to
        another pack in the same invocation.
        """
        if type(interval_seconds) is not int or interval_seconds <= 0:
            raise PublicationError("Интервал публикации должен быть положительным целым числом секунд.")
        with collection_lock(self.state.path.parent):
            active = self.state.get("daily_active_pack")
            if active is not None:
                if (not isinstance(active, dict) or not isinstance(active.get("short_name"), str)
                        or not isinstance(active.get("manifest_path"), str)
                        or not Path(active["manifest_path"]).is_absolute()
                        or not isinstance(active.get("manifest_hash"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", active["manifest_hash"])
                        or type(active.get("interval_seconds")) is not int or active["interval_seconds"] <= 0
                        or ("scheduled_for" in active and
                            (type(active["scheduled_for"]) not in {int, float}
                             or not math.isfinite(active["scheduled_for"]) or active["scheduled_for"] < 0))):
                    raise ReconciliationRequired("Повреждён checkpoint ежедневной публикации.")
                manifest_path = Path(active["manifest_path"])
            else:
                manifest_path = Path(manifest_path).resolve()
            archived_packs = {row["short_name"]: row["pack_hash"] for row in self.state.db.execute(
                "SELECT short_name,pack_hash FROM packs WHERE status='published'")}
            document, manifest_hash = _validate_manifest(manifest_path, archived_packs=archived_packs)
            if active and active["manifest_hash"] != manifest_hash:
                raise ReconciliationRequired("Manifest незавершённой ежедневной публикации изменён.")
            checked_config = _validate_config(config)
            self._freeze_plan(document, checked_config, manifest_hash)
            if active:
                row = self.state.db.execute("SELECT * FROM packs WHERE short_name=?",
                                            (active["short_name"],)).fetchone()
                if row is None:
                    raise ReconciliationRequired("Незавершённый ежедневный пак отсутствует в журнале.")
                if row["status"] == "published":
                    posts = self.state.db.execute("SELECT status FROM posts WHERE pack_name=?",
                                                   (active["short_name"],)).fetchall()
                    if not posts or any(post["status"] != "sent" for post in posts):
                        raise ReconciliationRequired("Опубликованный ежедневный пак содержит неподтверждённые сообщения.")
                    return self._complete_daily(active)
            else:
                next_due = self.state.get("daily_next_due_at")
                if next_due is not None:
                    if type(next_due) not in {int, float} or not math.isfinite(next_due) or next_due < 0:
                        raise ReconciliationRequired("Повреждено время следующей ежедневной публикации.")
                    if self.now() < next_due:
                        return self._daily_result("not_due")
                row = self.state.db.execute("SELECT * FROM packs WHERE status!='published' ORDER BY number LIMIT 1").fetchone()
                if row is None:
                    return self._daily_result("empty")
            name = row["short_name"]
            deferred = self.state.get("deferred")
            if deferred and deferred["not_before"] > self.now():
                return self._daily_result("deferred", name, not_before=deferred["not_before"],
                                          operation=deferred["operation"])
            if deferred:
                self.state.put("deferred", None)
            if active is None:
                active = {"short_name": name, "manifest_path": str(manifest_path),
                          "manifest_hash": manifest_hash, "interval_seconds": interval_seconds,
                          "scheduled_for": next_due if next_due is not None else self.now()}
                self.state.put("daily_active_pack", active)
            pack = next((pack for pack in document["packs"] if pack["short_name"] == name), None)
            if pack is None:
                raise ReconciliationRequired("Ежедневный пак отсутствует в закреплённом manifest.")
            try:
                account, channel, capabilities = await self._publication_context(document, checked_config)
                await self._pack(channel, pack, bool(account.get("premium")), document["brand"],
                                 capabilities["max_caption_length"])
            except DeferredPublication as error:
                return self._daily_result("deferred", name, not_before=error.not_before, operation=error.operation)
            return self._complete_daily(active)

    def _freeze_plan(self, document, config, manifest_hash):
        stored = self.state.get("config")
        if stored is not None:
            stored = dict(stored)
            stored.setdefault("photo_previews", False)
            stored.setdefault("welcome_enabled", True)
        if stored is not None and stored != config:
            username_only = {key for key in stored.keys() | config.keys() if stored.get(key) != config.get(key)} == {"username"}
            unpublished = self.state.db.execute("SELECT 1 FROM posts LIMIT 1").fetchone() is None
            if not (username_only and self.state.get("channel") is not None
                    and not self.state.get("channel_configured", False) and unpublished):
                raise PublicationError("Настройки канала отличаются от сохранённого плана публикации.")
        # Capacity is an export choice for new packs; frozen pack payloads below
        # still cannot be changed when the next export uses another capacity.
        series = {key: document.get(key) for key in ("schema_version", "adapter_version", "task_id", "brand", "prefix")}
        existing_series = self.state.get("series")
        if existing_series is not None:
            existing_series = {key: value for key, value in existing_series.items() if key != "pack_size"}
        if existing_series is not None and existing_series != series:
            raise PublicationError("Manifest относится к другой серии emoji.")
        previous = list(self.state.db.execute("SELECT * FROM packs ORDER BY number"))
        packs = document["packs"]
        if len(previous) > len(packs):
            raise PublicationError("Новый manifest удаляет ранее запланированный пак.")
        for old, pack in zip(previous, packs):
            if old["pack_hash"] != _hash(_json(pack).encode()):
                raise PublicationError(f"Замороженный пак {old['short_name']} изменился.")
        with self.state.db:
            for key, value in (("config", config), ("series", series), ("manifest_hash", manifest_hash)):
                self.state.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))
            for pack in packs[len(previous):]:
                payload = _json(pack)
                self.state.db.execute("INSERT INTO packs(number,short_name,pack_hash,payload) VALUES(?,?,?,?)", (pack["number"], pack["short_name"], _hash(payload.encode()), payload))

    async def _channel(self, config):
        channel = self.state.get("channel")
        if channel is None:
            matches = await self._call("find_channel", self.transport.owned_channels, config["title"], config["about"])
            if len(matches) > 1:
                raise ReconciliationRequired("Найдено несколько собственных каналов с этими названием и описанием. Автоматический выбор остановлен.")
            if matches:
                channel = matches[0]
            else:
                if self.state.get("channel_stage") == "creating":
                    raise ReconciliationRequired("Предыдущий запрос создания канала мог выполниться, но канал пока не найден. Повторное создание остановлено для защиты от дублей.")
                self.state.put("channel_stage", "creating")
                try:
                    channel = await self._call("create_channel", self.transport.create_channel, config["title"], config["about"])
                except DeferredPublication:
                    # Telegram explicitly rejected this request before creating anything.
                    self.state.put("channel_stage", "planned")
                    raise
                except errors.RPCError as error:
                    # Explicit client/auth/rate-limit rejections did not create a
                    # channel. Server errors (5xx) remain ambiguous, as do lost replies.
                    if 400 <= error.code < 500:
                        self.state.put("channel_stage", "planned")
                    raise
            if not isinstance(channel, dict) or type(channel.get("id")) is not int or not channel.get("access_hash"):
                raise ReconciliationRequired("Создание канала вернуло неполный результат; требуется сверка с Telegram.")
            self.state.put("channel", channel)
            self.state.put("channel_stage", "created")
        if not self.state.get("channel_configured", False):
            try:
                channel = await self._call("configure_channel", self.transport.configure_channel, channel, config["username"], config["avatar"])
            except errors.UsernameOccupiedError as error:
                raise PublicationError(f"Адрес канала @{config['username']} уже занят. Случайный альтернативный адрес не выбран.") from error
            except errors.UsernameInvalidError as error:
                raise PublicationError("Telegram отклонил запланированный адрес канала.") from error
            self.state.put("channel", channel)
            self.state.put("channel_configured", True)
        else:
            verified = await self._call("verify_channel", self.transport.verify_channel, channel, config)
            if not verified:
                raise ReconciliationRequired("Сохранённый канал больше не совпадает с планом или не принадлежит этому аккаунту. Публикация остановлена; проверьте владельца, название, описание и username канала.")
        return channel

    def _insert_post(self, key, pack_name, part, body, documents=None, links=None, photo_path=None, photo_sha256=None, formatting=None):
        self.state.db.execute("INSERT INTO posts(key,pack_name,part,random_id,body,documents,links,photo_path,photo_sha256,formatting) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (key, pack_name, part, secrets.randbelow(2**63 - 1) + 1,
                               body, _json(documents or []), _json(links or []), photo_path, photo_sha256, _json(formatting or [])))

    def _plan_welcome(self, brand):
        if self.state.db.execute("SELECT 1 FROM posts WHERE key='welcome'").fetchone():
            return
        body = (f"{brand}\nМузыка, которую видно.\n\n"
                "Обложки виниловых релизов становятся Telegram emoji. "
                "Каждый выпуск — отдельный нумерованный пак; под ним список альбомов со ссылками на первоисточники.\n\n"
                "Целая обложка, без обрезки и надписей поверх изображения.\n"
                "Выбирайте выпуск, добавляйте пак и собирайте свою коллекцию.")
        with self.state.db:
            self._insert_post("welcome", None, 0, body)

    async def _post(self, channel, key):
        post = self.state.db.execute("SELECT * FROM posts WHERE key=?", (key,)).fetchone()
        if post["photo_path"]:
            _read_checked(post["photo_path"], post["photo_sha256"], 10 * 1024 * 1024)
        if post["status"] == "sent":
            exists = await self._call(f"verify_post:{key}", self.transport.post_exists, channel, post["message_id"])
            if not exists:
                raise ReconciliationRequired(f"Опубликованное сообщение {post['message_id']} отсутствует. Автоматическое восстановление отключено, чтобы не создавать повторы.")
            return
        if post["status"] == "sending":
            found = await self._call(f"find_post:{key}", self.transport.find_post, channel, post["body"])
            if found:
                if type(found.get("id")) is not int or found["id"] <= 0 or not _https(found.get("link")):
                    raise ReconciliationRequired("Сверка сообщения Telegram вернула неполный результат.")
                with self.state.db:
                    self.state.db.execute("UPDATE posts SET status='sent',message_id=?,url=? WHERE key=?", (found["id"], found["link"], key))
                await self._checkpoint()
                return
        with self.state.db:
            self.state.db.execute("UPDATE posts SET status='sending' WHERE key=?", (key,))
        photo_options = {"photo_path": post["photo_path"]} if post["photo_path"] else {}
        if json.loads(post["formatting"]):
            photo_options["formatting"] = json.loads(post["formatting"])
        result = await self._call(f"send_post:{key}", self.transport.send_post,
                                  channel, post["body"], post["random_id"],
                                  json.loads(post["documents"]), json.loads(post["links"]), **photo_options)
        if not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] <= 0 or not _https(result.get("link")):
            raise ReconciliationRequired("Telegram мог отправить сообщение, но не вернул его ID. Сохранённый random_id будет использован при повторе.")
        with self.state.db:
            self.state.db.execute("UPDATE posts SET status='sent',message_id=?,url=? WHERE key=?", (result["id"], result["link"], key))
        await self._checkpoint()

    def _check_set(self, remote, pack, expected):
        if (not isinstance(remote, dict) or not remote.get("creator") or not remote.get("emojis")
                or remote.get("short_name") != pack["short_name"] or remote.get("title") != pack["title"]
                or not expected or not isinstance(remote.get("documents"), list)
                or [item.get("id") for item in remote["documents"]] != [item.get("id") for item in expected]):
            raise ReconciliationRequired(f"Пак {pack['short_name']} уже существует, но его владелец, тип, название или порядок emoji не совпадают с журналом.")

    async def _accept_created_set(self, remote, pack, expected):
        """Bind server-converted emoji IDs only after comparing the actual art.

        Telegram may convert generic uploads into new WebP documents on set
        creation. After this one-time reconciliation the canonical IDs are fixed.
        A previously published or externally replaced set must never be rebound.
        """
        row = self.state.db.execute("SELECT status,remote_set FROM packs WHERE short_name=?", (pack["short_name"],)).fetchone()
        identity_matches = (isinstance(remote, dict) and remote.get("creator") and remote.get("emojis")
            and remote.get("short_name") == pack["short_name"] and remote.get("title") == pack["title"]
            and expected and isinstance(remote.get("documents"), list)
            and len(remote["documents"]) == len(expected) == len(pack["items"]))
        if identity_matches:
            actual_ids = [item.get("id") for item in remote["documents"]]
            expected_ids = [item.get("id") for item in expected]
            if (actual_ids != expected_ids and row["status"] == "creating" and row["remote_set"] is None
                    and all(type(doc_id) is int and doc_id > 0 for doc_id in actual_ids)
                    and len(set(actual_ids)) == len(actual_ids)):
                verified = await self._call(f"verify_converted_images:{pack['short_name']}",
                                            self.transport.verify_set_content, remote, pack)
                if verified:
                    expected = remote["documents"]
                    with self.state.db:
                        self.state.db.execute("UPDATE packs SET documents=? WHERE short_name=?",
                                              (_json(expected), pack["short_name"]))
        self._check_set(remote, pack, expected)

    async def _pack(self, channel, pack, premium, brand, caption_limit=1024):
        name = pack["short_name"]
        keywords = [_search_keywords(item) for item in pack["items"]]
        row = self.state.db.execute("SELECT * FROM packs WHERE short_name=?", (name,)).fetchone()
        expected = json.loads(row["documents"]) if row["documents"] else None
        remote = await self._call(f"get_set:{name}", self.transport.get_set, name)
        if remote is not None:
            await self._accept_created_set(remote, pack, expected)
        else:
            if row["status"] in {"created", "published"}:
                raise ReconciliationRequired(f"Ранее созданный пак {name} больше не найден; автоматическое повторное создание остановлено.")
            if expected is None:
                expected = []
                for item in pack["items"]:
                    uploaded = self.state.db.execute("SELECT document FROM uploads WHERE sha256=?", (item["webp_sha256"],)).fetchone()
                    if uploaded:
                        reference = json.loads(uploaded[0])
                    else:
                        reference = await self._call(f"upload_emoji:{item['webp_sha256'][:16]}", self.transport.upload_emoji, item["webp_path"])
                        if not isinstance(reference, dict) or type(reference.get("id")) is not int:
                            raise PublicationError("Загрузка emoji не вернула ID документа Telegram.")
                        with self.state.db:
                            self.state.db.execute("INSERT INTO uploads VALUES(?,?)", (item["webp_sha256"], _json(reference)))
                    expected.append(reference)
                with self.state.db:
                    self.state.db.execute("UPDATE packs SET documents=? WHERE short_name=?", (_json(expected), name))
            with self.state.db:
                self.state.db.execute("UPDATE packs SET status='creating' WHERE short_name=?", (name,))
            try:
                remote = await self._call(f"create_set:{name}", self.transport.create_set, pack["title"], name, expected, pack["emoji"], keywords=keywords)
            except (errors.FileReferenceExpiredError, errors.FileReferenceEmptyError) as error:
                # This explicit rejection did not create a set. Refresh on the next run.
                with self.state.db:
                    for item in pack["items"]:
                        self.state.db.execute("DELETE FROM uploads WHERE sha256=?", (item["webp_sha256"],))
                    self.state.db.execute("UPDATE packs SET status='planned',documents=NULL WHERE short_name=?", (name,))
                raise PublicationError("Telegram отклонил устаревшие ссылки на загруженные emoji. Они будут загружены заново при следующем publish.") from error
            await self._accept_created_set(remote, pack, expected)
        # Read-before-write makes keyword changes resumable after a lost reply.
        # Each returned set must retain the bound document IDs and order.
        bound_documents = remote["documents"]
        for index, desired in enumerate(keywords):
            current = remote["documents"][index]
            if sorted(word.casefold() for word in current.get("keywords", [])) != sorted(word.casefold() for word in filter(None, desired.split(","))):
                remote = await self._call(f"keywords:{name}:{index + 1}",
                                          self.transport.change_keywords, current, desired)
                self._check_set(remote, pack, bound_documents)
        with self.state.db:
            self.state.db.execute("UPDATE packs SET status=?,remote_set=? WHERE short_name=?", ("published" if row["status"] == "published" else "created", _json(remote), name))
        if not row["thumbnail_done"]:
            await self._call(f"set_thumbnail:{name}", self.transport.set_thumbnail, name, remote["documents"][0]["id"])
            with self.state.db:
                self.state.db.execute("UPDATE packs SET thumbnail_done=1 WHERE short_name=?", (name,))
        existing_posts = list(self.state.db.execute("SELECT key FROM posts WHERE pack_name=? ORDER BY part", (name,)))
        if not existing_posts:
            if self.state.get("config").get("photo_previews", False):
                bodies = _publication_posts(pack, remote["documents"], premium, brand,
                                             channel["username"], self.state.path.parent / "previews" / name,
                                             caption_limit=caption_limit)
            else:
                bodies = _pack_posts(pack, remote["documents"], premium, brand)
            with self.state.db:
                for part, body in enumerate(bodies, 1):
                    self._insert_post(f"pack:{name}:{part}", name, part, **body)
            existing_posts = list(self.state.db.execute("SELECT key FROM posts WHERE pack_name=? ORDER BY part", (name,)))
        for post in existing_posts:
            await self._post(channel, post["key"])
        with self.state.db:
            self.state.db.execute("UPDATE packs SET status='published' WHERE short_name=?", (name,))


def _pack_posts(pack, documents, premium, brand):
    heading = f"{brand} / {pack['number']:03d}"
    footer = f"\nДобавить пак → https://t.me/addemoji/{pack['short_name']}"
    text = heading + f"\n\n{len(pack['items'])} обложек виниловых релизов.\n\n"
    emoji_documents = []
    if premium:
        if len(documents) != len(pack["items"]) or any(not isinstance(doc.get("alt"), str) or not doc["alt"] for doc in documents):
            raise PublicationError("Telegram не вернул actual alt для emoji; графическое объявление остановлено.")
        emoji_documents = [{"id": doc["id"], "alt": doc["alt"]} for doc in documents]
        text += "\n".join(" ".join(doc["alt"] for doc in documents[start:start + 5]) for start in range(0, len(documents), 5)) + "\n\n"
    links, posts = [], []
    if _units(text + footer) > 4000:
        raise PublicationError("Сетка emoji не помещается в сообщение Telegram.")
    for item in pack["items"]:
        album = _label(f"{item.get('artist') or 'Неизвестный артист'} — {item.get('title') or 'Без названия'}")
        prefix = f"{item['position']:02d} · "
        line = prefix + album + "\n"
        if _units(text + line + footer) > 4000:
            posts.append({"body": text.rstrip() + "\n" + footer, "documents": emoji_documents, "links": links})
            text = heading + " · продолжение\n\n"
            emoji_documents, links = [], []
        links.append({"offset": _units(text + prefix), "length": _units(album), "url": item["source_url"]})
        text += line
    posts.append({"body": text.rstrip() + "\n" + footer, "documents": emoji_documents, "links": links})
    return posts


def _publication_posts(pack, documents, premium, brand, channel_username, preview_dir, *, caption_limit=1024):
    """Each photo carries its own complete album list, emoji and pack link.

    Partition by both caption capacity and photo readability before rendering,
    so a large pack never leaves the album list in a separate text message.
    Artist/album names stay complete and searchable instead of becoming tags.
    """
    from .pack_preview import MAX_COVERS_PER_PHOTO, generate_preview
    if type(caption_limit) is not int or caption_limit < 1:
        raise PublicationError("Telegram не вернул корректный лимит подписи к фото.")
    if premium and (len(documents) != len(pack["items"])
                    or any(not isinstance(doc.get("alt"), str) or not doc["alt"] for doc in documents)):
        raise PublicationError("Telegram не вернул actual alt для emoji; графическое объявление остановлено.")

    def caption(start, end):
        items = pack["items"][start:end]
        count = len(pack["items"])
        scope = f"{count} обложек виниловых релизов." if start == 0 and end == count else f"Обложки {start + 1:02d}–{end:02d} из {count}."
        heading = f"✦ {brand} / {pack['number']:03d}"
        metadata = f"@{channel_username}\n{scope}"
        text = f"{heading}\n{metadata}\n\n"
        formatting = [{"type": "bold", "offset": 0, "length": _units(heading)},
                      {"type": "italic", "offset": _units(heading + "\n"), "length": _units(metadata)}]
        selected = [{"id": doc["id"], "alt": doc["alt"]} for doc in documents[start:end]] if premium else []
        if selected:
            text += "\n".join(" ".join(doc["alt"] for doc in selected[i:i + 5])
                              for i in range(0, len(selected), 5)) + "\n\n"
        links = []
        for item in items:
            album = " ".join(f"{item.get('artist') or 'Неизвестный артист'} — {item.get('title') or 'Без названия'}".split())
            text += f"{item['position']:02d} · "
            links.append({"offset": _units(text), "length": _units(album), "url": item["source_url"]})
            text += album + "\n"
        text += "\n┄┄┄\n♡ "
        action = "Добавить пак ↗"
        links.append({"offset": _units(text), "length": _units(action),
                      "url": f"https://t.me/addemoji/{pack['short_name']}"})
        formatting.append({"type": "bold", "offset": _units(text), "length": _units(action)})
        text += action
        return {"body": text, "documents": selected, "links": links, "formatting": formatting}

    posts, page_sizes = [], []
    start, count = 0, len(pack["items"])
    while start < count:
        # Check complete candidates rather than truncating album names. Near a
        # boundary the whole-pack heading can be shorter than a page heading.
        candidates = [(end, caption(start, end)) for end in range(start + 1, min(count, start + MAX_COVERS_PER_PHOTO) + 1)]
        fitting = [(end, post) for end, post in candidates if _units(post["body"]) <= caption_limit]
        if not fitting:
            raise PublicationError(f"Полное название альбома #{pack['items'][start]['position']} не помещается в подпись Telegram.")
        end, post = fitting[-1]
        page_sizes.append(end - start)
        posts.append(post)
        start = end
    previews = generate_preview(pack, preview_dir, channel_username, page_sizes=page_sizes)
    for post, preview in zip(posts, previews, strict=True):
        post.update(photo_path=preview["path"], photo_sha256=preview["sha256"])
    return posts

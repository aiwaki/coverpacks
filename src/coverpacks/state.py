"""SQLite checkpoint; atomic writes on one event loop, ordered image commits."""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def collection_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".collector.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Эта база уже используется другим процессом coverpacks.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class State:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ("covers","thumbnails","exports","gallery"):
            (self.root / directory).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if columns and "cursor" not in columns:
            backup_dir = self.root / "backups"
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            with sqlite3.connect(backup_dir / f"before-bandcamp-{stamp}.sqlite3") as backup:
                self.db.backup(backup)
            with self.db:
                self.db.execute("ALTER TABLE tasks ADD COLUMN cursor TEXT NOT NULL DEFAULT '*'")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
                config TEXT NOT NULL, target INTEGER NOT NULL, max_candidates INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0, discovered INTEGER NOT NULL DEFAULT 0,
                exhausted TEXT, stop_reason TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
                cursor TEXT NOT NULL DEFAULT '*'
            );
            CREATE TABLE IF NOT EXISTS entities (
                kind TEXT NOT NULL, mbid TEXT NOT NULL, metadata TEXT NOT NULL,
                PRIMARY KEY (kind, mbid)
            );
            CREATE TABLE IF NOT EXISTS entries (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                kind TEXT NOT NULL, mbid TEXT NOT NULL, metadata TEXT NOT NULL,
                group_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                excluded INTEGER NOT NULL DEFAULT 0, result TEXT,
                UNIQUE(task_id, kind, mbid)
            );
            CREATE INDEX IF NOT EXISTS queue ON entries(task_id,status,seq);
            CREATE INDEX IF NOT EXISTS groups ON entries(task_id,group_id,status);
            CREATE TABLE IF NOT EXISTS blobs (
                sha256 TEXT PRIMARY KEY, metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_images (
                release_mbid TEXT NOT NULL, image_id TEXT NOT NULL,
                sha256 TEXT NOT NULL REFERENCES blobs(sha256), metadata TEXT NOT NULL,
                PRIMARY KEY(release_mbid,image_id)
            );
            CREATE TABLE IF NOT EXISTS image_links (
                seq INTEGER NOT NULL REFERENCES entries(seq),
                release_mbid TEXT NOT NULL, image_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(seq,release_mbid,image_id,sha256)
            );
            CREATE TABLE IF NOT EXISTS external_source_images (
                source TEXT NOT NULL, entity_id TEXT NOT NULL, image_id TEXT NOT NULL,
                sha256 TEXT NOT NULL REFERENCES blobs(sha256), metadata TEXT NOT NULL,
                PRIMARY KEY(source,entity_id,image_id)
            );
            CREATE TABLE IF NOT EXISTS external_image_links (
                seq INTEGER NOT NULL REFERENCES entries(seq), source TEXT NOT NULL,
                entity_id TEXT NOT NULL, image_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(seq,source,entity_id,image_id,sha256)
            );
            CREATE TABLE IF NOT EXISTS bandcamp_pages (
                task_id INTEGER NOT NULL REFERENCES tasks(id), cursor TEXT NOT NULL,
                PRIMARY KEY(task_id,cursor)
            );
        """)

    def close(self):
        self.db.close()

    def task(self, task_id=None):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if task_id else self.db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("Задача не найдена. Сначала выполните collect.")
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def create_task(self, config, target=None, max_candidates=None):
        fingerprint = hashlib.sha256(encoded(config).encode()).hexdigest()
        with self.db:
            default_target = target or 25
            self.db.execute("INSERT OR IGNORE INTO tasks(fingerprint,config,target,max_candidates) VALUES(?,?,?,?)", (fingerprint, encoded(config), default_target, max_candidates or max(250,default_target*20)))
            task_id = self.db.execute("SELECT id FROM tasks WHERE fingerprint=?", (fingerprint,)).fetchone()[0]
            if target is not None:
                self.db.execute("UPDATE tasks SET target=? WHERE id=?",(target,task_id))
            if max_candidates is not None:
                self.db.execute("UPDATE tasks SET max_candidates=? WHERE id=?",(max_candidates,task_id))
        return self.task(task_id)

    def set_task(self, task_id, **values):
        allowed = {"target", "max_candidates", "stop_reason", "exhausted"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Некорректное изменение задачи")
        with self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?", (*values.values(), task_id))

    def stage_page(self, task_id, items, next_offset, exhausted=None):
        # Persist the ENTIRE page and its cursor in the same transaction.
        with self.db:
            for kind, metadata in items:
                mbid = metadata["id"]
                self.db.execute("INSERT INTO entities VALUES(?,?,?) ON CONFLICT(kind,mbid) DO UPDATE SET metadata=excluded.metadata", (kind,mbid,encoded(metadata)))
                group_id = mbid if kind == "release-group" else metadata.get("release-group", {}).get("id")
                self.db.execute("INSERT OR IGNORE INTO entries(task_id,kind,mbid,metadata,group_id) VALUES(?,?,?,?,?)", (task_id,kind,mbid,encoded(metadata),group_id))
            self.db.execute("UPDATE tasks SET offset=?,discovered=discovered+?,exhausted=? WHERE id=?", (next_offset,len(items),exhausted,task_id))

    def queue(self, task_id, limit=1):
        return [dict(row) for row in self.db.execute("SELECT * FROM entries WHERE task_id=? AND status='pending' ORDER BY seq LIMIT ?", (task_id,limit))]

    def repairs(self, task_id, limit=1):
        return [dict(row) for row in self.db.execute("SELECT * FROM entries WHERE task_id=? AND status='pending' AND result IS NOT NULL ORDER BY seq LIMIT ?", (task_id,limit))]

    def _stage_bandcamp_entries(self, task_id, items):
        # Caller owns the transaction; existing entry snapshots stay untouched.
        added = 0
        for metadata in items:
            entity_id = metadata["id"]
            self.db.execute("INSERT INTO entities VALUES(?,?,?) ON CONFLICT(kind,mbid) DO UPDATE SET metadata=excluded.metadata", ("bandcamp-album",entity_id,encoded(metadata)))
            added += self.db.execute("INSERT OR IGNORE INTO entries(task_id,kind,mbid,metadata) VALUES(?,?,?,?)", (task_id,"bandcamp-album",entity_id,encoded(metadata))).rowcount
        return added

    def stage_bandcamp_page(self, task_id, items, next_cursor, exhausted=None):
        # The legacy mbid column is an internal identifier slot. Bandcamp IDs
        # never enter MusicBrainz provenance columns or the exported entity_mbid.
        with self.db:
            cursor = self.task(task_id)["cursor"]
            self.db.execute("INSERT INTO bandcamp_pages VALUES(?,?)", (task_id,cursor))
            self._stage_bandcamp_entries(task_id, items)
            self.db.execute("UPDATE tasks SET cursor=?,discovered=discovered+?,exhausted=? WHERE id=?", (next_cursor or cursor,len(items),exhausted,task_id))

    def stage_bandcamp_head_page(self, task_id, items):
        """Append newly seen IDs without replacing the normal scan checkpoint."""
        with self.db:
            if self.task(task_id)["config"].get("source") != "bandcamp":
                raise ValueError("Обновление Discover доступно только для задачи Bandcamp")
            return self._stage_bandcamp_entries(task_id, items)

    def bandcamp_cursors(self, task_id):
        return {row[0] for row in self.db.execute("SELECT cursor FROM bandcamp_pages WHERE task_id=?", (task_id,))}

    def recover(self, task_id):
        with self.db:
            self.db.execute("UPDATE entries SET status='pending' WHERE task_id=? AND status='processing'", (task_id,))

    def begin(self, seq):
        with self.db:
            self.db.execute("UPDATE entries SET status='processing',attempts=attempts+1,last_error=NULL WHERE seq=?", (seq,))

    def remember(self, kind, metadata):
        with self.db:
            self.db.execute("INSERT INTO entities VALUES(?,?,?) ON CONFLICT(kind,mbid) DO UPDATE SET metadata=excluded.metadata",(kind,metadata["id"],encoded(metadata)))

    def enrich(self, seq, group):
        row = self.db.execute("SELECT metadata FROM entries WHERE seq=?",(seq,)).fetchone()
        data = json.loads(row[0])
        for key in ("title","artist-credit","first-release-date"):
            if group.get(key):
                data[key] = group[key]
        with self.db:
            self.db.execute("UPDATE entries SET metadata=?,group_id=? WHERE seq=?",(encoded(data),group["id"],seq))

    def failed(self, seq, status, message, group_id=None):
        with self.db:
            self.db.execute("UPDATE entries SET status=?,last_error=?,group_id=COALESCE(?,group_id) WHERE seq=?", (status,message,group_id,seq))

    def group_done(self, task_id, group_id, seq=None):
        return self.db.execute("SELECT seq FROM entries WHERE task_id=? AND group_id=? AND status IN ('downloaded','needs_review','duplicate_image') AND seq != ?", (task_id,group_id,seq or -1)).fetchone() is not None

    def success(self, seq, result, *, duplicate_group=False):
        task_id = self.db.execute("SELECT task_id FROM entries WHERE seq=?",(seq,)).fetchone()[0]
        duplicate = self.db.execute("SELECT 1 FROM entries WHERE task_id=? AND seq!=? AND status IN ('downloaded','needs_review','duplicate_image') AND json_extract(result,'$.sha256')=? LIMIT 1",(task_id,seq,result["sha256"])).fetchone() is not None
        status = "duplicate_group" if duplicate_group else "duplicate_image" if duplicate else "needs_review" if result.get("needs_review") else "downloaded"
        with self.db:
            self.db.execute("INSERT INTO blobs VALUES(?,?) ON CONFLICT(sha256) DO UPDATE SET metadata=excluded.metadata", (result["sha256"],encoded({key:result.get(key) for key in ("path","thumbnail_path","width","height","size_bytes","format")})))
            if result.get("source") == "bandcamp":
                identity = (result["source"],result["source_entity_id"],result["source_image_id"])
                self.db.execute("INSERT INTO external_source_images VALUES(?,?,?,?,?) ON CONFLICT(source,entity_id,image_id) DO UPDATE SET sha256=excluded.sha256,metadata=excluded.metadata", (*identity,result["sha256"],encoded(result)))
                self.db.execute("INSERT OR IGNORE INTO external_image_links VALUES(?,?,?,?,?)", (seq,*identity,result["sha256"]))
            else:
                self.db.execute("INSERT INTO source_images VALUES(?,?,?,?) ON CONFLICT(release_mbid,image_id) DO UPDATE SET sha256=excluded.sha256,metadata=excluded.metadata", (result["release_mbid"],result["caa_image_id"],result["sha256"],encoded(result)))
                self.db.execute("INSERT OR IGNORE INTO image_links VALUES(?,?,?,?)", (seq,result["release_mbid"],result["caa_image_id"],result["sha256"]))
            self.db.execute("UPDATE entries SET group_id=?,result=?,status=?,last_error=NULL WHERE seq=?", (result.get("release_group_mbid"),encoded(result),status,seq))

    def cached_external_source(self, source, entity_id, image_id, variant):
        row = self.db.execute("SELECT metadata FROM external_source_images WHERE source=? AND entity_id=? AND image_id=?", (source,entity_id,str(image_id))).fetchone()
        if row:
            info = json.loads(row[0])
            if info.get("actual_quality") == variant and self.valid_file(info):
                return info
        return None

    def cached_source(self, release_id, image_id, variant):
        row = self.db.execute("SELECT metadata FROM source_images WHERE release_mbid=? AND image_id=?",(release_id,str(image_id))).fetchone()
        if row:
            info = json.loads(row[0])
            if info.get("actual_quality") == variant and self.valid_file(info):
                return info
        return None

    def valid_file(self, row):
        path = self.root / row["path"]
        try:
            with path.open("rb") as file:
                return hashlib.file_digest(file,"sha256").hexdigest() == row["sha256"]
        except OSError:
            return False

    def rows(self, task_id=None):
        query = "SELECT * FROM entries" + (" WHERE task_id=?" if task_id else "") + " ORDER BY seq"
        rows = []
        for item in self.db.execute(query,(task_id,) if task_id else ()):
            meta = json.loads(item["metadata"])
            row = {key:None for key in ("release_mbid","caa_image_id","path","thumbnail_path","format","width","height","size_bytes","sha256","image_url","downloaded_url","front","approved","selection_reason","requested_quality","actual_quality","source_url")}
            row.update({"date":meta.get("first-release-date") or meta.get("date") or None,"title":meta.get("title"),"artist":artist_credit(meta),"release_group_mbid":item["group_id"],"needs_review":False,"review_reasons":[]})
            row.update(json.loads(item["result"]) if item["result"] else {})
            row.update({key:item[key] for key in ("seq","task_id","status","attempts","last_error")})
            row.update({"excluded":bool(item["excluded"]),"entity_type":item["kind"],"entity_mbid":item["mbid"]})
            row["entity_id"] = item["mbid"]
            if item["kind"] == "bandcamp-album":
                row.update({"source":"bandcamp","entity_mbid":None,"release_group_mbid":None,"release_mbid":None,"caa_image_id":None})
                if not item["result"]:
                    row.update({"artist":meta.get("album_artist") or meta.get("band_name"),"date":meta.get("release_date"),"source_url":meta.get("item_url")})
            else:
                row["source"] = "musicbrainz"
            rows.append(row)
        return rows

    def count(self, task_id):
        return self.db.execute("SELECT COUNT(DISTINCT json_extract(result,'$.sha256')) FROM entries WHERE task_id=? AND status IN ('downloaded','needs_review','duplicate_image')",(task_id,)).fetchone()[0]

    def retry(self, task_id, include_not_found=False):
        states = ["network_error","blocked","http_error","invalid_json","invalid_image","too_large","too_many_pixels","missing_file","corrupt_file","api_error"]
        if include_not_found:
            states += ["not_found","no_front","no_suitable_release"]
        with self.db:
            result = self.db.execute("UPDATE entries SET status='pending' WHERE task_id=? AND status IN (" + ",".join("?" for _ in states) + ")",(task_id,*states))
        return result.rowcount

    def exclude(self, seq, value=True):
        with self.db:
            changed = self.db.execute("UPDATE entries SET excluded=? WHERE seq=?",(int(value),seq)).rowcount
        if not changed:
            raise ValueError(f"seq={seq} не найден")

    def audit_files(self, task_id, *, archived_hashes=()):
        """Audit local files, except hashes the caller verified in its archive."""
        archived_hashes = frozenset(archived_hashes)
        if any(not isinstance(digest, str) or len(digest) != 64
               or any(char not in "0123456789abcdef" for char in digest)
               for digest in archived_hashes):
            raise ValueError("Архив должен содержать корректные SHA-256")
        broken = 0
        for row in self.rows(task_id):
            if row["status"] not in {"downloaded","needs_review","duplicate_image"}:
                continue
            if row["sha256"] in archived_hashes:
                continue
            if not self.valid_file(row) or not (self.root / row["thumbnail_path"]).is_file():
                self.failed(row["seq"],"pending","Файл отсутствует или изменён; требуется восстановление")
                broken += 1
        return broken


def artist_credit(metadata):
    parts = metadata.get("artist-credit") or []
    return "".join(item if isinstance(item,str) else (item.get("name") or item.get("artist",{}).get("name") or "") + (item.get("joinphrase") or "") for item in parts) or None

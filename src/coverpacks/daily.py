"""One resumable daily batch; the catalogue and the publication ledger persist."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from .bandcamp import BandcampAPI, BandcampCollector
from .emoji import _item_metadata, _selected, _source_path, album_identity, prepare
from .network import Network
from .publisher import Publisher, _hash, _json


def archived_packs(ledger):
    return {row["short_name"]: row["pack_hash"] for row in ledger.db.execute(
        "SELECT short_name,pack_hash FROM packs WHERE status='published'")}


def archived_hashes(ledger):
    result = set()
    for row in ledger.db.execute("SELECT payload,pack_hash FROM packs WHERE status='published'"):
        payload = json.loads(row["payload"])
        if _hash(_json(payload).encode()) != row["pack_hash"]:
            raise ValueError("Published pack ledger changed")
        result.update(item["source_sha256"] for item in payload["items"])
    return result


def manifest_path(root, prefix):
    directory = root / "emoji" / prefix
    pointer = json.loads((directory / "latest.json").read_bytes())
    name = pointer["manifest"]
    if Path(name).name != name:
        raise ValueError("Invalid manifest pointer")
    path = directory / name
    if _hash(path.read_bytes()) != pointer["sha256"]:
        raise ValueError("Manifest pointer checksum mismatch")
    return path


def needed_media(state, ledger, config):
    """Historical published originals stay archived, not on the runner's disk."""
    archived = archived_hashes(ledger)
    paths = {Path(config["avatar"])}
    series = ledger.get("series") or {}
    for row in state.rows(series.get("task_id")):
        if row.get("sha256") in archived or not row.get("path"):
            continue
        for key in ("path", "thumbnail_path"):
            if row.get(key):
                paths.add(state.root / row[key])
    for row in ledger.db.execute("SELECT payload FROM packs WHERE status!='published'"):
        for item in json.loads(row["payload"])["items"]:
            paths.update(Path(item[key]) for key in ("source_path", "png_path", "webp_path"))
    if series.get("prefix"):
        document = json.loads(manifest_path(state.root, series["prefix"]).read_bytes())
        published = archived_packs(ledger)
        for pack in document["packs"]:
            if pack["short_name"] not in published:
                for item in pack["items"]:
                    paths.update(Path(item[key]) for key in ("source_path", "png_path", "webp_path"))
    for row in ledger.db.execute("SELECT photo_path FROM posts WHERE pack_name IN (SELECT short_name FROM packs WHERE status!='published')"):
        if row[0]:
            paths.add(Path(row[0]))
    return paths


def verify_selection(state, document):
    current = {row["seq"]: row for row in _selected(state.rows(document["task_id"]))}
    for pack in document["packs"]:
        for item in pack["items"]:
            row = current.get(item["seq"])
            if row is None:
                raise ValueError("A frozen cover is no longer selected")
            metadata = _item_metadata(row, _source_path(state.root, row))
            if any(item.get(key) != value for key, value in metadata.items()):
                raise ValueError("Frozen cover metadata changed; publication stopped")


def _ready_count(state, task_id, document, capacity):
    frozen_items = [item for pack in document["packs"] for item in pack["items"]]
    frozen = {item["source_sha256"] for item in frozen_items}
    albums = {identity for item in frozen_items if (identity := album_identity(item)) is not None}
    ready = 0
    for row in _selected(state.rows(task_id)):
        if row["sha256"] in frozen:
            continue
        identity = album_identity(row)
        if identity is not None:
            if identity in albums:
                continue
            albums.add(identity)
        ready += 1
        if ready >= capacity:
            break
    return ready


def preparation_due_at(ledger, settings):
    """Open a bounded preparation window before a server-scheduled release."""
    ahead = settings.get("schedule_ahead_seconds", 0)
    if type(ahead) is not int or not 0 <= ahead <= 86400:
        raise ValueError("Schedule preparation window must be 0..86400 seconds")
    due = ledger.get("daily_next_due_at")
    if due is None:
        return 0
    if type(due) not in {int, float} or not math.isfinite(due) or due < 0:
        raise ValueError("Invalid daily publication timestamp")
    return due - ahead


async def run_daily(state, ledger, config, settings, transport_factory, checkpoint, *, now=time.time,
                    collector_factory=None):
    task_id, prefix = settings["task_id"], settings["prefix"]
    capacity = settings.get("pack_size", 200)
    if type(capacity) is not int or not 1 <= capacity <= 200:
        raise ValueError("Daily pack capacity must be 1..200")
    active = ledger.get("daily_active_pack")
    ready_at = preparation_due_at(ledger, settings)
    if not active and now() < ready_at:
        return {"status": "not_due", "next_due_at": ledger.get("daily_next_due_at")}
    manifest = manifest_path(state.root, prefix)
    document = json.loads(manifest.read_bytes())
    published = archived_packs(ledger)
    pending = any(pack["short_name"] not in published for pack in document["packs"])
    if not active and not pending and now() < (ledger.get("bandcamp_not_before") or 0):
        return {"status": "source_deferred", "not_before": ledger.get("bandcamp_not_before")}
    if (active or pending) and ledger.get("daily_collection") is not None:
        # prepare() installs latest atomically before the collection intent is
        # cleared. Recover that exact crash window without reusing yesterday's
        # head_done flag and fixed collection target on the following day.
        ledger.put("daily_collection", None)
        await checkpoint()
    if not active and not pending:
        cycle = ledger.get("daily_collection")
        if cycle is None:
            ready = _ready_count(state, task_id, document, capacity)
            task = state.task(task_id)
            cycle = {"target": state.count(task_id) + max(0, capacity - ready),
                     "max_candidates": task["discovered"] + settings.get("candidate_budget", 800),
                     "head_done": False}
            ledger.put("daily_collection", cycle)
            await checkpoint()
        state.set_task(task_id, target=cycle["target"], max_candidates=cycle["max_candidates"])
        def record_cooldown(collector, report=None):
            previous = ledger.get("bandcamp_not_before") or 0
            deadline = max(previous, getattr(collector, "rate_limited_until", None) or 0,
                           (report or {}).get("not_before") or 0)
            if deadline > previous:
                ledger.put("bandcamp_not_before", deadline)
                return True
            return False

        async def collect(collector):
            if not cycle["head_done"]:
                report = await collector.refresh_head(task_id, max_pages=settings.get("head_pages", 4))
                cycle["head_done"] = True
                cycle["head_report"] = report
                ledger.put("daily_collection", cycle)
                record_cooldown(collector, report)
                await checkpoint()
                if report.get("stopped") == "rate_limited":
                    return
            await collector.run(task_id, archived_hashes=archived_hashes(ledger))
            if record_cooldown(collector):
                await checkpoint()
        if collector_factory:
            await collect(collector_factory(state))
        else:
            async with Network(settings["contact"], state.root, concurrency=3) as network:
                await collect(BandcampCollector(state, BandcampAPI(network), network))
        ready = _ready_count(state, task_id, document, capacity)
        if ready < capacity:
            # Keep the downloaded covers, but never freeze a partial daily pack.
            # A completed short collection needs a new head refresh and target
            # next time: raw download counts can include duplicate albums.
            ledger.put("daily_collection", None)
            await checkpoint()
            if now() < (ledger.get("bandcamp_not_before") or 0):
                return {"status": "source_deferred", "not_before": ledger.get("bandcamp_not_before"),
                        "ready": ready, "required": capacity}
            return {"status": "waiting_for_covers", "ready": ready, "required": capacity,
                    "next_due_at": ledger.get("daily_next_due_at")}
        manifest = prepare(state, task_id, pack_size=capacity, prefix=prefix,
                           archived_packs=published, max_new_items=capacity)
        # A crash after this commit resumes the prepared manifest, never collects
        # another 200 images into a second pack for this same daily slot.
        ledger.put("daily_collection", None)
        await checkpoint()
        document = json.loads(manifest.read_bytes())
        if not any(pack["short_name"] not in published for pack in document["packs"]):
            if now() < (ledger.get("bandcamp_not_before") or 0):
                return {"status": "source_deferred", "not_before": ledger.get("bandcamp_not_before")}
            return {"status": "empty", "next_due_at": ledger.get("daily_next_due_at")}
    selected_document = json.loads(Path(active["manifest_path"]).read_bytes()) if active else document
    verify_selection(state, selected_document)
    async with transport_factory() as transport:
        result = await Publisher(ledger, transport, now=now, checkpoint=checkpoint).publish_next(
            manifest, config, interval_seconds=settings.get("interval_seconds", 86400),
            schedule_ahead_seconds=settings.get("schedule_ahead_seconds", 0))
    await checkpoint()
    return result

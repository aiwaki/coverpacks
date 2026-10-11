"""Daily orchestration against real local state and a fake Telegram service."""
import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from coverpacks import daily
from coverpacks.emoji import prepare
from coverpacks.publisher import PublicationError, PublicationState, Publisher
from coverpacks.state import State
from test_publisher import FakeTelegram


def add_cover(state, task_id, number, *, artist=None, title=None):
    entity_id = f"bandcamp:album:10:{number}"
    artist = f"Artist {number}" if artist is None else artist
    title = f"Album {number}" if title is None else title
    url = f"https://artist{number}.bandcamp.com/album/test"
    state.stage_bandcamp_head_page(task_id, [{
        "id": entity_id, "title": title, "album_artist": artist,
        "item_url": url, "release_date": "2026-10-06",
    }])
    seq = state.db.execute("SELECT seq FROM entries WHERE task_id=? AND mbid=?", (task_id, entity_id)).fetchone()[0]
    output = io.BytesIO()
    Image.new("RGB", (32, 32), ((number >> 16) & 255, (number >> 8) & 255, number & 255)).save(output, "PNG")
    content = output.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    original, thumb = state.root / "covers" / f"{digest}.png", state.root / "thumbnails" / f"{digest}.png"
    original.write_bytes(content)
    thumb.write_bytes(content)
    state.success(seq, {
        "source": "bandcamp", "source_entity_id": entity_id, "source_image_id": str(number),
        "sha256": digest, "path": str(original.relative_to(state.root)),
        "thumbnail_path": str(thumb.relative_to(state.root)), "format": "PNG",
        "width": 32, "height": 32, "size_bytes": len(content),
        "artist": artist, "title": title, "source_url": url,
        "needs_review": False, "review_reasons": [],
    })
    return seq


class FakeCollector:
    def __init__(self, case):
        self.case = case
        self.head_calls, self.run_calls = [], []
        self.on_head = self.on_run = None
        self.next_number = 1000
        self.rate_limited_until = None

    async def refresh_head(self, task_id, *, max_pages):
        self.rate_limited_until = None
        self.head_calls.append((task_id, max_pages))
        if self.on_head:
            return self.on_head()
        return {"pages": 1, "examined": 0, "added": 0, "known": 0, "stopped": "end_of_results"}

    def add_one(self):
        add_cover(self.case.state, self.case.task_id, self.next_number)
        self.next_number += 1

    async def run(self, task_id, *, archived_hashes):
        self.rate_limited_until = None
        self.run_calls.append({"target": self.case.state.task(task_id)["target"], "archived": set(archived_hashes)})
        if self.on_run:
            return self.on_run()
        while self.case.state.count(task_id) < self.case.state.task(task_id)["target"]:
            self.add_one()


@pytest.fixture
def case(tmp_path, request, monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("Daily unit tests must not open the real network")
    monkeypatch.setattr(daily, "Network", forbid_network)
    state = State(tmp_path / "data")
    count = getattr(request, "param", 1)
    task_id = state.create_task({"source": "bandcamp", "mode": "vinyl"}, count, 120)["id"]
    for number in range(1, count + 1):
        add_cover(state, task_id, number)
    avatar = state.root / "avatar.png"
    Image.new("RGB", (32, 32), "pink").save(avatar)
    config = {"title": "SLEEVE", "about": "", "username": "sleeve_test",
              "avatar": str(avatar), "welcome_enabled": False, "photo_previews": False}
    manifest = prepare(state, task_id)
    ledger = PublicationState(state.root / "telegram")
    transport = FakeTelegram()
    asyncio.run(Publisher(ledger, transport).run(manifest, config))
    transport.events.clear()
    value = SimpleNamespace(state=state, task_id=task_id, ledger=ledger, transport=transport,
                            config=config, original_manifest=json.loads(manifest.read_bytes()),
                            settings={"task_id": task_id, "prefix": "sleeve_vinyl", "pack_size": 2,
                                      "head_pages": 4, "candidate_budget": 800, "interval_seconds": 86400},
                            timestamp=1000, transport_opens=0, collector_opens=0,
                            checkpoints=0, on_checkpoint=None)
    value.collector = FakeCollector(value)

    @asynccontextmanager
    async def transport_factory():
        value.transport_opens += 1
        yield value.transport

    async def checkpoint():
        value.checkpoints += 1
        if value.on_checkpoint:
            value.on_checkpoint()

    value.transport_factory, value.checkpoint = transport_factory, checkpoint
    yield value
    ledger.close()
    state.close()


def run(case):
    def collector_factory(state):
        assert state is case.state
        case.collector_opens += 1
        return case.collector
    return asyncio.run(daily.run_daily(
        case.state, case.ledger, case.config, case.settings, case.transport_factory,
        case.checkpoint, now=lambda: case.timestamp, collector_factory=collector_factory))


def test_not_due_opens_no_transport_collector_or_checkpoint(case):
    case.ledger.put("daily_next_due_at", case.timestamp + 60)
    (case.state.root / "emoji" / "sleeve_vinyl" / "latest.json").unlink()
    result = run(case)
    assert result == {"status": "not_due", "next_due_at": 1060}
    assert case.transport_opens == case.checkpoints == 0
    assert case.collector.head_calls == case.collector.run_calls == []
    assert case.transport.events == []


@pytest.mark.parametrize("failure_point", ["after_head_checkpoint", "during_collection"])
def test_resumed_cycle_does_not_repeat_head_or_increase_target(case, failure_point):
    if failure_point == "after_head_checkpoint":
        def stop():
            cycle = case.ledger.get("daily_collection")
            if cycle and cycle["head_done"]:
                case.on_checkpoint = None
                raise OSError("checkpoint response lost")
        case.on_checkpoint = stop
    else:
        def stop():
            case.collector.add_one()
            case.collector.on_run = None
            raise OSError("collection interrupted")
        case.collector.on_run = stop
    with pytest.raises(OSError):
        run(case)
    cycle = copy.deepcopy(case.ledger.get("daily_collection"))
    assert cycle["head_done"] is True and cycle["target"] == 3
    assert len(case.collector.head_calls) == 1 and case.transport_opens == 0
    if failure_point == "after_head_checkpoint":
        # New local progress after the failure must not move this slot's goal.
        case.collector.add_one()
    result = run(case)
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
    assert [entry["target"] for entry in case.collector.run_calls] == [3] * len(case.collector.run_calls)
    assert len(case.collector.head_calls) == 1
    assert case.state.count(case.task_id) == 3
    assert case.ledger.get("daily_collection") is None
    assert case.ledger.get("daily_next_due_at") == case.timestamp + 86400


def test_crash_after_prepare_resumes_frozen_manifest_without_collecting_again(case, monkeypatch):
    prepare_calls = []
    real_prepare = daily.prepare
    def traced_prepare(*args, **kwargs):
        path = real_prepare(*args, **kwargs)
        prepare_calls.append(path)
        return path
    monkeypatch.setattr(daily, "prepare", traced_prepare)
    def fail_after_prepare():
        document = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
        if len(document["packs"]) > 1 and case.ledger.get("daily_collection") is None:
            case.on_checkpoint = None
            raise OSError("crash after manifest commit")
    case.on_checkpoint = fail_after_prepare
    with pytest.raises(OSError, match="manifest commit"):
        run(case)
    frozen_path = prepare_calls[0]
    frozen_bytes = frozen_path.read_bytes()
    assert case.ledger.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 1
    assert case.transport_opens == 0
    add_cover(case.state, case.task_id, 2000)
    assert run(case)["pack"] == "sleeve_vinyl_002"
    assert len(prepare_calls) == len(case.collector.head_calls) == len(case.collector.run_calls) == 1
    assert daily.manifest_path(case.state.root, "sleeve_vinyl") == frozen_path
    assert frozen_path.read_bytes() == frozen_bytes
    assert case.ledger.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 2


def test_crash_before_collection_cleanup_does_not_reuse_yesterday_target(case, monkeypatch):
    real_prepare = daily.prepare
    prepare_calls = []
    def stop_after_manifest_commit(*args, **kwargs):
        path = real_prepare(*args, **kwargs)
        prepare_calls.append(path)
        if len(prepare_calls) == 1:
            raise OSError("prepared manifest saved before collection cleanup")
        return path
    monkeypatch.setattr(daily, "prepare", stop_after_manifest_commit)
    with pytest.raises(OSError, match="before collection cleanup"):
        run(case)
    stale_cycle = copy.deepcopy(case.ledger.get("daily_collection"))
    assert stale_cycle["head_done"] is True and stale_cycle["target"] == 3
    assert run(case)["pack"] == "sleeve_vinyl_002"
    assert case.ledger.get("daily_collection") is None
    assert len(prepare_calls) == len(case.collector.head_calls) == 1
    case.timestamp += 86400
    assert run(case)["pack"] == "sleeve_vinyl_003"
    assert [call["target"] for call in case.collector.run_calls] == [3, 5]
    assert len(prepare_calls) == len(case.collector.head_calls) == 2


@pytest.mark.parametrize("change", ["excluded", "title", "sha256"])
@pytest.mark.parametrize("which_pack", ["published", "pending"])
def test_prepared_pending_pack_rechecks_catalogue_selection_before_transport(case, monkeypatch, change, which_pack):
    real_prepare = daily.prepare
    def stop_after_manifest_commit(*args, **kwargs):
        path = real_prepare(*args, **kwargs)
        raise OSError("prepared but not published")
    monkeypatch.setattr(daily, "prepare", stop_after_manifest_commit)
    with pytest.raises(OSError, match="not published"):
        run(case)
    monkeypatch.setattr(daily, "prepare", real_prepare)
    seq = case.state.rows(case.task_id)[0 if which_pack == "published" else -1]["seq"]
    if change == "excluded":
        case.state.exclude(seq)
    else:
        row = case.state.db.execute("SELECT result FROM entries WHERE seq=?", (seq,)).fetchone()
        result = json.loads(row[0])
        result[change] = "0" * 64 if change == "sha256" else "Edited title"
        with case.state.db:
            case.state.db.execute("UPDATE entries SET result=? WHERE seq=?", (json.dumps(result), seq))
    with pytest.raises((ValueError, PublicationError)):
        run(case)
    assert case.transport_opens == 0 and case.transport.events == []


@pytest.mark.parametrize("capacity", [0, 201, True, 1.5])
def test_invalid_daily_capacity_stops_before_any_work(case, capacity):
    case.settings["pack_size"] = capacity
    before = case.state.task(case.task_id)
    with pytest.raises(ValueError, match="capacity"):
        run(case)
    assert case.state.task(case.task_id) == before
    assert case.ledger.get("daily_collection") is None
    assert case.transport_opens == case.checkpoints == 0
    assert case.collector.head_calls == case.collector.run_calls == []


@pytest.mark.parametrize("case", [25], indirect=True)
def test_daily_prepares_only_one_pack_of_200_and_keeps_published_001(case):
    old = copy.deepcopy(case.original_manifest["packs"][0])
    for item in old["items"]:
        Path(item["source_path"]).unlink()
    for number in range(26, 236):
        add_cover(case.state, case.task_id, number)
    case.settings["pack_size"] = 200
    result = run(case)
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
    document = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
    assert document["packs"][0] == old
    assert [len(pack["items"]) for pack in document["packs"]] == [25, 200]
    assert [item["seq"] for item in document["packs"][1]["items"]] == list(range(26, 226))
    assert case.state.count(case.task_id) == 235  # ten ready covers remain for another day
    assert [event[1] for event in case.transport.events if event[0] == "create_set"] == ["sleeve_vinyl_002"]
    assert len([event for event in case.transport.events if event[0] == "upload_emoji"]) == 200
    assert case.collector.run_calls[0]["archived"] == {item["source_sha256"] for item in old["items"]}


def test_repeated_unchanged_head_has_no_duplicate_entries_or_publications(case):
    old = case.original_manifest["packs"][0]["items"][0]
    old_row = case.state.rows(case.task_id)[0]
    def known_head():
        added = case.state.stage_bandcamp_head_page(case.task_id, [{
            "id": old["source_entity_id"], "title": "New upstream title", "album_artist": "Changed",
        }])
        return {"pages": 1, "examined": 1, "added": added, "known": 1 - added, "stopped": "end_of_results"}
    case.collector.on_head = known_head
    case.collector.on_run = lambda: None
    first = run(case)
    case.timestamp += 86400
    second = run(case)
    assert first["status"] == second["status"] == "empty"
    assert len(case.collector.head_calls) == 2
    assert case.state.rows(case.task_id) == [old_row]
    assert case.transport_opens == 0 and case.transport.events == []
    assert case.ledger.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 1


def test_needed_media_includes_prepared_pack_before_ledger_and_omits_published_originals(case):
    old = case.original_manifest["packs"][0]["items"][0]
    old_row = case.state.rows(case.task_id)[0]
    Path(old["source_path"]).unlink()
    add_cover(case.state, case.task_id, 2)
    new_manifest = prepare(case.state, case.task_id, archived_packs=daily.archived_packs(case.ledger), max_new_items=200)
    document = json.loads(new_manifest.read_bytes())
    pending = document["packs"][1]["items"][0]
    assert case.ledger.db.execute("SELECT COUNT(*) FROM packs WHERE short_name='sleeve_vinyl_002'").fetchone()[0] == 0
    needed = daily.needed_media(case.state, case.ledger, case.config)
    assert Path(case.config["avatar"]) in needed
    assert {Path(pending[key]) for key in ("source_path", "png_path", "webp_path")} <= needed
    assert Path(old["source_path"]) not in needed
    assert case.state.root / old_row["thumbnail_path"] not in needed
    assert Path(old["png_path"]) not in needed and Path(old["webp_path"]) not in needed


def test_archive_selection_rejects_published_payload_hash_drift(case):
    with case.ledger.db:
        case.ledger.db.execute("UPDATE packs SET payload='{}' WHERE status='published'")
    with pytest.raises(ValueError, match="ledger changed"):
        daily.needed_media(case.state, case.ledger, case.config)
    assert case.transport_opens == 0 and case.collector.head_calls == []


def test_source_cooldown_without_prepared_work_opens_nothing(case):
    deadline = case.timestamp + 300
    case.ledger.put("bandcamp_not_before", deadline)
    before = case.state.rows(case.task_id), case.state.task(case.task_id)
    result = run(case)
    assert result["status"] == "source_deferred" and result["not_before"] == deadline
    assert case.transport_opens == case.collector_opens == case.checkpoints == 0
    assert case.collector.head_calls == case.collector.run_calls == []
    assert case.transport.events == []
    assert (case.state.rows(case.task_id), case.state.task(case.task_id)) == before


@pytest.mark.parametrize("active", [False, True])
def test_prepared_pending_or_active_pack_publishes_despite_source_cooldown(case, active):
    add_cover(case.state, case.task_id, 2)
    prepared = prepare(case.state, case.task_id, pack_size=2,
                       archived_packs=daily.archived_packs(case.ledger), max_new_items=2)
    deadline = case.timestamp + 300
    case.ledger.put("bandcamp_not_before", deadline)
    if active:
        case.ledger.put("daily_active_pack", {
            "short_name": "sleeve_vinyl_002", "manifest_path": str(prepared),
            "manifest_hash": hashlib.sha256(prepared.read_bytes()).hexdigest(),
            "interval_seconds": 86400,
        })
        case.ledger.put("daily_next_due_at", case.timestamp + 86400)
    result = run(case)
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
    assert case.collector_opens == 0
    assert case.collector.head_calls == case.collector.run_calls == []
    assert case.transport_opens == 1
    assert [event[1] for event in case.transport.events if event[0] == "create_set"] == ["sleeve_vinyl_002"]
    assert case.ledger.get("bandcamp_not_before") == deadline


@pytest.mark.parametrize("ready_cover", [False, True])
@pytest.mark.parametrize("deadline_source", ["report", "collector"])
def test_head_rate_limit_checkpoints_deadline_skips_collection_and_uses_ready_covers(case, ready_cover, deadline_source):
    if ready_cover:
        add_cover(case.state, case.task_id, 2)
    deadline = case.timestamp + 90
    snapshots = []
    def limited_head():
        report = {"pages": 1, "examined": 0, "added": 0, "known": 0,
                  "stopped": "rate_limited", "error": "Bandcamp HTTP 429", "retry_after": 90}
        if deadline_source == "collector":
            case.collector.rate_limited_until = deadline
        else:
            report["not_before"] = deadline
        return report
    def snapshot():
        snapshots.append((case.ledger.get("bandcamp_not_before"),
                          copy.deepcopy(case.ledger.get("daily_collection")),
                          len(case.collector.run_calls), case.transport_opens))
    case.collector.on_head = limited_head
    case.on_checkpoint = snapshot
    result = run(case)
    assert case.collector.run_calls == []
    assert case.ledger.get("bandcamp_not_before") == deadline
    assert any(saved == deadline and cycle and cycle.get("head_report", {}).get("stopped") == "rate_limited"
               and runs == opened == 0 for saved, cycle, runs, opened in snapshots)
    if ready_cover:
        assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
        document = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
        assert [item["seq"] for item in document["packs"][1]["items"]] == [2]
        assert case.transport_opens == 1
    else:
        assert result["status"] == "source_deferred" and result["not_before"] == deadline
        assert case.transport_opens == 0 and case.transport.events == []
        before = case.checkpoints, case.collector_opens, len(case.collector.head_calls)
        again = run(case)
        assert again["status"] == "source_deferred" and again["not_before"] == deadline
        assert (case.checkpoints, case.collector_opens, len(case.collector.head_calls)) == before


def test_collection_rate_limit_checkpoints_deadline_and_next_invocation_waits(case):
    deadline = case.timestamp + 120
    checkpoint_deadlines = []
    def limited_collection():
        case.collector.rate_limited_until = deadline
        return "Сбор остановлен: rate_limited"
    case.collector.on_run = limited_collection
    case.on_checkpoint = lambda: checkpoint_deadlines.append(case.ledger.get("bandcamp_not_before"))
    result = run(case)
    assert result["status"] == "source_deferred" and result["not_before"] == deadline
    assert deadline in checkpoint_deadlines
    assert case.ledger.get("bandcamp_not_before") == deadline
    assert case.transport_opens == 0
    before = case.checkpoints, case.collector_opens, len(case.collector.head_calls), len(case.collector.run_calls)
    again = run(case)
    assert again["status"] == "source_deferred" and again["not_before"] == deadline
    assert (case.checkpoints, case.collector_opens, len(case.collector.head_calls), len(case.collector.run_calls)) == before
    case.timestamp = deadline + 1
    case.collector.on_run = None
    assert run(case)["pack"] == "sleeve_vinyl_002"
    assert len(case.collector.run_calls) == 2


@pytest.mark.parametrize("stage", ["head", "collection"])
def test_crash_at_source_deadline_checkpoint_resumes_with_no_requests_during_cooldown(case, stage):
    deadline = case.timestamp + 120
    def limit():
        case.collector.rate_limited_until = deadline
        if stage == "head":
            return {"pages": 0, "examined": 0, "added": 0, "known": 0,
                    "stopped": "rate_limited", "error": "Bandcamp HTTP 429", "not_before": deadline}
        return "rate_limited"
    if stage == "head":
        case.collector.on_head = limit
    else:
        case.collector.on_run = limit
    def crash():
        if case.ledger.get("bandcamp_not_before") == deadline:
            case.on_checkpoint = None
            raise OSError("deadline checkpoint response lost")
    case.on_checkpoint = crash
    with pytest.raises(OSError, match="deadline checkpoint"):
        run(case)
    assert case.ledger.get("bandcamp_not_before") == deadline
    before = case.checkpoints, case.collector_opens, len(case.collector.head_calls), len(case.collector.run_calls)
    result = run(case)
    assert result["status"] == "source_deferred" and result["not_before"] == deadline
    assert (case.checkpoints, case.collector_opens, len(case.collector.head_calls), len(case.collector.run_calls)) == before
    assert case.transport_opens == 0 and case.transport.events == []


def test_saved_daily_target_ignores_normalized_album_duplicates_in_frozen_and_ready_batch(case):
    add_cover(case.state, case.task_id, 2, artist=" ＡＲＴＩＳＴ　１ ", title=" ａｌｂｕｍ\t１ ")
    first_new = add_cover(case.state, case.task_id, 3, artist="Fresh Artist", title="New Album")
    add_cover(case.state, case.task_id, 4, artist=" ＦＲＥＳＨ\t ARTIST ", title=" new　ALBUM ")
    assert len({row["sha256"] for row in case.state.rows(case.task_id)}) == 4
    case.settings["pack_size"] = 3
    snapshots = []
    def snapshot():
        cycle = case.ledger.get("daily_collection")
        if cycle:
            snapshots.append((copy.deepcopy(cycle), len(case.collector.head_calls), len(case.collector.run_calls)))
    case.on_checkpoint = snapshot
    result = run(case)
    assert snapshots[0][0]["target"] == 6  # four stored images + two genuinely new albums needed
    assert snapshots[0][1:] == (0, 0)
    assert [call["target"] for call in case.collector.run_calls] == [6]
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
    document = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
    assert document["packs"][0] == case.original_manifest["packs"][0]
    assert len(document["packs"][1]["items"]) == 3
    assert document["packs"][1]["items"][0]["seq"] == first_new
    assert case.state.count(case.task_id) == 6


def test_duplicates_arriving_during_collection_allow_partial_pack_and_next_day_progress(case):
    def collect_with_duplicate():
        add_cover(case.state, case.task_id, 1000, artist=" ARTIST 1 ", title="album　1")
        add_cover(case.state, case.task_id, 1001)
        case.collector.next_number = 1002
        case.collector.on_run = None
    case.collector.on_run = collect_with_duplicate
    first = run(case)
    assert first["status"] == "published" and first["pack"] == "sleeve_vinyl_002"
    assert [call["target"] for call in case.collector.run_calls] == [3]
    first_manifest = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
    assert [item["source_entity_id"] for item in first_manifest["packs"][1]["items"]] == ["bandcamp:album:10:1001"]
    case.timestamp += 86400
    second = run(case)
    assert second["status"] == "published" and second["pack"] == "sleeve_vinyl_003"
    assert [call["target"] for call in case.collector.run_calls] == [3, 5]
    final_manifest = json.loads(daily.manifest_path(case.state.root, "sleeve_vinyl").read_bytes())
    assert final_manifest["packs"][:2] == first_manifest["packs"]
    assert len(final_manifest["packs"][2]["items"]) == 2

"""Offline end-to-end coordinator checks; fixtures are not live API evidence."""

import asyncio
import hashlib
from io import BytesIO

import httpx
from PIL import Image
import pytest

from coverpacks.api import CoverOption, SearchPage
from coverpacks.collector import Collector
from coverpacks.network import Download, FetchError, Network
from coverpacks.state import State


def config(mode="albums", **changes):
    return {"mode": mode, "artist_id": None, "imports": [], "quality": "1200", "max_releases": 3, **changes}


def group(identity):
    return {"id": identity, "primary-type": "Album", "title": f"Album {identity}", "first-release-date": "2001", "artist-credit": [{"name": "Artist", "artist": {"id": "a1"}}]}


def release(identity, group_id, medium='12" Vinyl'):
    return {"id": identity, "release-group": {"id": group_id}, "media": [{"format": medium}], "date": "2002"}


def option(release_id, image_id="1", *, url=None, approved=True):
    url = url or f"https://images.example/{release_id}/{image_id}.png"
    return CoverOption(image_id, url, "1200", True, approved, release_id, "fixture front", url, "https://coverartarchive.org/release/fixture", "1200")


class FakeAPI:
    def __init__(self, groups=(), releases=(), pages=None, covers=None):
        self.groups = {item["id"]: item for item in groups}
        self.release_data = {item["id"]: item for item in releases}
        self.pages = pages or {}
        self.covers = covers or {}
        self.calls = []

    async def search_page(self, mode, artist_id, offset, limit):
        self.calls.append(("search", mode, offset, limit))
        page = self.pages[offset]
        if isinstance(page, Exception):
            raise page
        return page

    async def group(self, identity):
        self.calls.append(("group", identity))
        return self.groups[identity]

    async def release(self, identity):
        self.calls.append(("release", identity))
        return self.release_data[identity]

    async def releases(self, group_id, cap, vinyl):
        self.calls.append(("releases", group_id, cap, vinyl))
        return [item for item in self.release_data.values() if item["release-group"]["id"] == group_id][:cap]

    async def cover_options(self, kind, identity, quality):
        self.calls.append(("cover", kind, identity, quality))
        options = self.covers.get((kind, identity), FetchError("not_found", "CAA fixture 404"))
        if isinstance(options, Exception):
            raise options
        return options


class FakeNetwork:
    def __init__(self, root, payloads=None, delays=None):
        self.root = root
        self.payloads = payloads or {}
        self.delays = delays or {}
        self.calls = []
        self.completed = []
        self.max_bytes = 30 * 1024**2
        self.max_pixels = 40_000_000

    async def download(self, url):
        self.calls.append(url)
        await asyncio.sleep(self.delays.get(url, 0))
        payload = self.payloads.get(url, url.encode())
        if isinstance(payload, Exception):
            raise payload
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / "covers" / f"{digest}.png"
        thumbnail = self.root / "thumbnails" / f"{digest}.jpg"
        path.parent.mkdir(exist_ok=True)
        thumbnail.parent.mkdir(exist_ok=True)
        path.write_bytes(payload)
        thumbnail.write_bytes(b"preview")
        self.completed.append(url)
        return Download(digest, path, thumbnail, "PNG", 1200, 1200, len(payload), url)


@pytest.fixture
def state(tmp_path):
    current = State(tmp_path)
    yield current
    current.close()


def stage(state, settings, items, target=25, max_candidates=100):
    task = state.create_task(settings, target, max_candidates)
    state.stage_page(task["id"], items, len(items), "fixture exhausted")
    return task


def run(state, api, network, task, **kwargs):
    return asyncio.run(Collector(state, api, network, concurrency=3).run(task["id"], **kwargs))


def test_target_limit_persists_whole_page_then_resumes_without_duplicate_records(tmp_path):
    settings = config()
    groups = [group(f"g{i}") for i in range(5)]
    api = FakeAPI(groups, pages={0: SearchPage(groups, None, 5, "fixture exhausted")},
        covers={("release-group", item["id"]): [option(f"r{i}")] for i, item in enumerate(groups)})
    state = State(tmp_path)
    task = state.create_task(settings, 2, 100)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    before = [(row["seq"], row["entity_mbid"]) for row in state.rows(task["id"])]
    assert len(before) == 5
    assert len(network.calls) == state.count(task["id"]) == 2
    assert len(state.queue(task["id"], 10)) == 3
    assert state.task(task["id"])["offset"] == 5
    state.close()
    reopened = State(tmp_path)
    try:
        run(reopened, api, network, task)
        assert len(network.calls) == 2  # same target is total, not another batch
        changed = reopened.create_task(settings, 4, 100)
        run(reopened, api, network, changed)
        assert reopened.count(task["id"]) == len(network.calls) == 4
        assert [(row["seq"], row["entity_mbid"]) for row in reopened.rows(task["id"])] == before
        assert len([call for call in api.calls if call[0] == "search"]) == 1
    finally:
        reopened.close()


def test_pagination_uses_actual_endpoint_cursor_and_reaches_25_unique_covers(state):
    groups = [group(f"g{i}") for i in range(27)]
    pages = {0: SearchPage(groups[:11], 11, 27), 11: SearchPage(groups[11:19], 19, 27), 19: SearchPage(groups[19:], None, 27, "fixture exhausted")}
    api = FakeAPI(groups, pages=pages, covers={("release-group", item["id"]): [option(f"r{i}")] for i, item in enumerate(groups)})
    task = state.create_task(config(), 25, 100)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert state.count(task["id"]) == 25
    assert [call[2] for call in api.calls if call[0] == "search"] == [0, 11, 19]
    assert [call[3] for call in api.calls if call[0] == "search"] == [25,25,25]
    assert state.task(task["id"])["max_candidates"] == 100
    assert len(state.rows(task["id"])) == 27
    run(state, api, network, task)
    assert len(network.calls) == 25


def test_concurrency_completion_order_cannot_renumber_or_choose_later_group_release(state):
    first, second, third = option("r1"), option("r2"), option("r3")
    api = FakeAPI([group("g1"), group("g2"), group("g3")],
        covers={("release-group", "g1"): [first], ("release-group", "g2"): [second], ("release-group", "g3"): [third]})
    task = stage(state, config(), [("release-group", {"id": f"g{i}"}) for i in (1, 2, 3)], target=3)
    network = FakeNetwork(state.root, delays={first.url: .025, second.url: .01})
    original_seqs = [row["seq"] for row in state.rows(task["id"])]
    run(state, api, network, task)
    assert network.completed == [third.url, second.url, first.url]
    rows = state.rows(task["id"])
    assert [row["seq"] for row in rows] == original_seqs
    assert [row["release_group_mbid"] for row in rows] == ["g1", "g2", "g3"]


def test_vinyl_deduplicates_editions_in_same_group_in_discovery_order(state):
    seed1, seed2 = release("r1", "g1"), release("r2", "g1")
    first, second = option("r1"), option("r2")
    api = FakeAPI([group("g1")], [seed1, seed2], covers={("release", "r1"): [first], ("release", "r2"): [second]})
    task = stage(state, config("vinyl"), [("release", seed1), ("release", seed2)], target=2)
    network = FakeNetwork(state.root, delays={first.url: .02})
    run(state, api, network, task)
    rows = state.rows(task["id"])
    assert state.count(task["id"]) == 1
    assert [row["status"] for row in rows] == ["downloaded", "duplicate_group"]
    assert rows[0]["release_mbid"] == "r1"


def test_parallel_discarded_edition_retains_downloaded_source_relationship(state):
    seed1, seed2 = release("r1", "g1"), release("r2", "g1")
    api = FakeAPI([group("g1")], [seed1, seed2], covers={("release", "r1"): [option("r1")], ("release", "r2"): [option("r2")]})
    task = stage(state, config("vinyl"), [("release", seed1), ("release", seed2)], target=2)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert len(network.calls) == 2
    links = state.db.execute("SELECT release_mbid FROM image_links ORDER BY release_mbid").fetchall()
    assert [row[0] for row in links] == ["r1", "r2"]


@pytest.mark.parametrize("medium", ["CD", "Digital Media", "Shellac", "Acetate", None])
def test_vinyl_never_downloads_non_vinyl_fallback(state, medium):
    seed, other = release("vinyl", "g1"), release("other", "g1", medium)
    api = FakeAPI([group("g1")], [seed, other], covers={("release", "other"): [option("other")], ("release-group", "g1"): [option("other")]})
    task = stage(state, config("vinyl"), [("release", seed)], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert network.calls == []
    assert ("cover", "release", "other", "1200") not in api.calls
    assert not any(call[:2] == ("cover", "release-group") for call in api.calls)
    assert state.rows(task["id"])[0]["status"] == "not_found"


def test_vinyl_tries_another_confirmed_vinyl_edition_when_seed_has_no_front(state):
    first, second = release("r1", "g1"), release("r2", "g1", '7" Vinyl')
    api = FakeAPI([group("g1")], [first, second], covers={("release", "r1"): [], ("release", "r2"): [option("r2")]})
    task = stage(state, config("vinyl"), [("release", first)], target=1)
    run(state, api, FakeNetwork(state.root), task)
    row = state.rows(task["id"])[0]
    assert row["release_mbid"] == "r2"
    assert row["media_formats"] == ['7" Vinyl']
    assert row["fallback_errors"][0]["status"] == "no_front"


def test_albums_falls_back_from_missing_representative_to_release(state):
    seed = release("r1", "g1", "CD")
    api = FakeAPI([group("g1")], [seed], covers={("release", "r1"): [option("r1")]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    run(state, api, FakeNetwork(state.root), task)
    row = state.rows(task["id"])[0]
    assert row["status"] == "downloaded" and row["release_mbid"] == "r1"
    assert row["release_group_mbid"] == "g1"
    assert row["fallback_errors"][0]["status"] == "not_found"
    assert api.calls.index(("cover", "release-group", "g1", "1200")) < api.calls.index(("cover", "release", "r1", "1200"))


def test_albums_uses_representative_before_imported_release(state):
    seed = release("r1", "g1", "CD")
    representative = option("representative")
    api = FakeAPI([group("g1")], [seed], covers={("release-group", "g1"): [representative], ("release", "r1"): [option("r1")]})
    task = stage(state, config(), [("release", seed)], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert network.calls == [representative.url]
    assert state.rows(task["id"])[0]["release_mbid"] == "representative"


def test_no_front_is_terminal_until_explicit_retry_and_keeps_attempt_count(state):
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): []})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    run(state, api, network, task)
    row = state.rows(task["id"])[0]
    assert row["status"] == "no_front" and row["attempts"] == 1
    assert network.calls == []


def test_candidate_budget_is_distinct_from_success_target(state):
    groups = [group("g1"), group("g2")]
    api = FakeAPI(groups, pages={0: SearchPage(groups, 2, 1000)})
    task = state.create_task(config(), 25, 2)
    reason = run(state, api, FakeNetwork(state.root), task)
    assert state.count(task["id"]) == 0
    assert "лимит просматриваемых кандидатов" in reason
    assert [call for call in api.calls if call[0] == "search"] == [("search", "albums", 0, 2)]
    assert state.task(task["id"])["discovered"] == 2


def test_search_failure_preserves_queue_and_cursor_for_retry(state):
    api = FakeAPI(pages={0: FetchError("network_error", "fixture timeout")})
    task = state.create_task(config(), 1, 10)
    reason = run(state, api, FakeNetwork(state.root), task)
    assert "Checkpoint сохранён" in reason
    assert state.task(task["id"])["offset"] == 0
    assert state.task(task["id"])["exhausted"] is None
    api.groups["g1"] = group("g1")
    api.pages[0] = SearchPage([group("g1")], None, 1, "fixture exhausted")
    api.covers[("release-group", "g1")] = [option("r1")]
    run(state, api, FakeNetwork(state.root), task)
    assert state.count(task["id"]) == 1


def test_retry_failed_does_not_discover_new_candidates(state):
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [option("r1")]})
    task = state.create_task(config(), 2, 10)
    state.stage_page(task["id"], [("release-group", {"id": "g1"})], 1)
    seq = state.rows(task["id"])[0]["seq"]
    state.failed(seq, "network_error", "fixture")
    state.retry(task["id"])
    run(state, api, FakeNetwork(state.root), task, discover=False)
    assert state.count(task["id"]) == 1
    assert not any(call[0] == "search" for call in api.calls)


def test_download_fallback_records_actual_quality_and_error(state):
    first = option("r1")
    fallback = CoverOption("1", first.url + "?500", "500", True, False, "r1", "fallback_from_1200", first.url, "metadata", "1200")
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [first, fallback]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root, payloads={first.url: FetchError("not_found", "missing 1200")})
    run(state, api, network, task)
    row = state.rows(task["id"])[0]
    assert row["requested_quality"] == "1200" and row["actual_quality"] == "500"
    assert row["needs_review"] is True and row["approved"] is False
    assert row["downloaded_url"] == fallback.url and row["image_url"] == first.url
    assert row["fallback_errors"] == [{"status": "not_found", "message": "missing 1200"}]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_real_network_preserves_bytes_and_repairs_same_entry(state, damage):
    output = BytesIO()
    Image.new("RGB", (800, 800), (25, 50, 75)).save(output, "PNG")
    content = output.getvalue()
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=content, headers={"content-type": "image/png"})
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [option("r1")]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    async def collect_once():
        async with Network("https://example.invalid/coverpacks-tests", state.root, transport=httpx.MockTransport(handler)) as network:
            await Collector(state, api, network).run(task["id"])
    asyncio.run(collect_once())
    before = state.rows(task["id"])[0]
    path = state.root / before["path"]
    assert path.read_bytes() == content
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged")
    asyncio.run(collect_once())
    after = state.rows(task["id"])[0]
    assert path.read_bytes() == content
    assert after["seq"] == before["seq"] and after["sha256"] == before["sha256"]
    assert after["attempts"] == 2 and after["status"] == "downloaded"
    assert len(state.rows(task["id"])) == 1 and len(calls) == 2


def test_repair_uses_saved_source_even_if_current_representative_changes(state):
    original, replacement = option("original"), option("replacement")
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [original]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    before = state.rows(task["id"])[0]
    (state.root / before["path"]).unlink()
    api.covers[("release-group", "g1")] = [replacement]
    run(state, api, network, task)
    after = state.rows(task["id"])[0]
    assert after["seq"] == before["seq"]
    assert after["release_mbid"] == "original"
    assert after["sha256"] == before["sha256"]
    assert network.calls == [original.url, original.url]


def test_repair_is_not_skipped_after_decreasing_collection_target(state):
    api = FakeAPI([group("g1"), group("g2")], covers={("release-group", "g1"): [option("r1")], ("release-group", "g2"): [option("r2")]})
    settings = config()
    task = stage(state, settings, [("release-group", {"id": "g1"}), ("release-group", {"id": "g2"})], target=2)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    path = state.root / state.rows(task["id"])[0]["path"]
    path.unlink()
    state.create_task(settings, 1, 100)
    run(state, api, network, task)
    assert path.is_file()
    assert all(row["status"] == "downloaded" for row in state.rows(task["id"]))


def test_release_fallback_obeys_per_group_cap(state):
    editions = [release(f"r{i}", "g1", "CD") for i in range(10)]
    api = FakeAPI([group("g1")], editions, covers={("release", "r9"): [option("r9")]})
    task = stage(state, config(max_releases=2), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert network.calls == []
    assert len([call for call in api.calls if call[0] == "release"]) == 2
    assert ("releases", "g1", 2, False) in api.calls


def test_cancelled_workers_recover_pending_entries_and_stable_seq(state):
    class InterruptOnce(FakeNetwork):
        interrupted = False

        async def download(self, url):
            if not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return await super().download(url)

    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [option("r1")]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = InterruptOnce(state.root)
    before = state.rows(task["id"])[0]
    with pytest.raises(asyncio.CancelledError):
        run(state, api, network, task)
    assert state.rows(task["id"])[0]["status"] == "processing"
    run(state, api, network, task)
    after = state.rows(task["id"])[0]
    assert after["seq"] == before["seq"]
    assert after["status"] == "downloaded" and after["attempts"] == 2


def test_source_image_is_reused_across_tasks_without_redownload(state):
    seed = release("r1", "g1")
    selected = option("r1")
    api = FakeAPI([group("g1")], [seed], covers={("release-group", "g1"): [selected], ("release", "r1"): [selected]})
    first = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, first)
    second = stage(state, config("vinyl"), [("release", seed)], target=1)
    run(state, api, network, second)
    assert network.calls == [selected.url]
    assert state.count(first["id"]) == state.count(second["id"]) == 1
    assert state.rows(first["id"])[0]["seq"] != state.rows(second["id"])[0]["seq"]
    assert state.db.execute("SELECT COUNT(*) FROM image_links").fetchone()[0] == 2


def test_changed_source_bytes_during_repair_are_flagged_for_review(state):
    selected = option("r1")
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [selected]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root, payloads={selected.url: b"first version"})
    run(state, api, network, task)
    previous = state.rows(task["id"])[0]
    (state.root / previous["path"]).unlink()
    network.payloads[selected.url] = b"changed upstream"
    run(state, api, network, task)
    current = state.rows(task["id"])[0]
    assert current["needs_review"] is True and current["status"] == "needs_review"
    assert current["previous_sha256"] == previous["sha256"]
    assert current["sha256"] != previous["sha256"]
    assert current["seq"] == previous["seq"]


def test_missing_thumbnail_is_repaired_without_new_entry(state):
    selected = option("r1")
    api = FakeAPI([group("g1")], covers={("release-group", "g1"): [selected]})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    previous = state.rows(task["id"])[0]
    thumbnail = state.root / previous["thumbnail_path"]
    thumbnail.unlink()
    run(state, api, network, task)
    assert thumbnail.is_file()
    assert state.rows(task["id"])[0]["seq"] == previous["seq"]
    assert len(state.rows(task["id"])) == 1


def test_import_queue_resumes_without_search(state):
    imports = [{"kind": "release-group", "mbid": "g1"}, {"kind": "release", "mbid": "r2"}]
    api = FakeAPI([group("g1"), group("g2")], [release("r2", "g2")], covers={("release-group", "g1"): [option("r1")], ("release-group", "g2"): [option("r2")]})
    settings = config(imports=imports)
    task = state.create_task(settings, 1, 10)
    network = FakeNetwork(state.root)
    run(state, api, network, task)
    assert len(state.rows(task["id"])) == 2
    assert state.count(task["id"]) == 1
    state.create_task(settings, 2, 10)
    run(state, api, network, task)
    assert state.count(task["id"]) == 2
    assert not any(call[0] == "search" for call in api.calls)


def test_mixed_vinyl_and_cd_bundle_requires_manual_review(state):
    seed = release("r1", "g1")
    seed["media"].append({"format": "CD"})
    api = FakeAPI([group("g1")], [seed], covers={("release", "r1"): [option("r1")]})
    task = stage(state, config("vinyl"), [("release", seed)], target=1)
    run(state, api, FakeNetwork(state.root), task)
    row = state.rows(task["id"])[0]
    assert row["needs_review"] is True
    assert any("Смешанный комплект" in reason for reason in row["review_reasons"])


def test_blocking_is_reported_distinctly_from_missing_front(state):
    api = FakeAPI([group("g1")], [release("r1", "g1", "CD")], covers={("release-group", "g1"): FetchError("blocked", "HTTP 403"), ("release", "r1"): []})
    task = stage(state, config(), [("release-group", {"id": "g1"})], target=1)
    run(state, api, FakeNetwork(state.root), task)
    assert state.rows(task["id"])[0]["status"] == "blocked"

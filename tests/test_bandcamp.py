"""Offline contracts from observed Bandcamp markup; no live requests in tests."""
import asyncio
import hashlib
from io import BytesIO

import httpx
from PIL import Image
import pytest

from coverpacks.api import ApiDataError
from coverpacks.bandcamp import (BandcampAPI, BandcampCollector, BandcampPage,
    DISCOVERY_API, QUALITY, album_url, parse_artwork)
from coverpacks.network import Download, FetchError, Network
from coverpacks.state import State


def item(number=1, **changes):
    return {"item_id": number, "item_type": "a", "result_type": "a", "band_id": 10,
        "title": f"Album {number}", "album_artist": "Album artist", "band_name": "Label",
        "release_date": "2026-10-02 00:00:00 UTC",
        "item_url": f"https://label.bandcamp.com/album/album-{number}?from=discover_page",
        "primary_image": {"image_id": number, "is_art": True},
        "package_info": [{"format": "vinyl", "package_id": number + 100, "title": "LP", "is_preorder": False}],
        **changes}


def art_url(number=1):
    return f"https://f4.bcbits.com/img/a{number:010d}_10.jpg"


def art_html(number=1, **changes):
    anchor = changes.get("anchor", art_url(number))
    preview = changes.get("preview", f"https://f4.bcbits.com/img/a{number:010d}_2.jpg")
    return f'<div id="tralbumArt"><a class="popupImage" href="{anchor}"><picture><source srcset="ignored.avif 2x"/><img src="{preview}"></picture></a></div>'


def page_data(items=None, cursor="next", **changes):
    items = items if items is not None else [item()]
    return {"results": items, "batch_result_count": len(items), "result_count": 100,
            "cursor": cursor, **changes}


class MetadataNetwork:
    def __init__(self, data=None, html=None):
        self.data = data if data is not None else page_data()
        self.html = html if html is not None else art_html()
        self.calls = []
        self.fresh_calls = []

    async def post_json(self, url, payload, *, fresh=False):
        self.calls.append((url, payload))
        self.fresh_calls.append(fresh)
        return self.data

    async def text(self, url):
        self.calls.append(url)
        return self.html


def sanitized(number=1, **changes):
    network = MetadataNetwork(page_data([item(number, **changes)]))
    return asyncio.run(BandcampAPI(network).discover()).items[0]


class FakeAPI:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []
        self.fresh_calls = []

    async def discover(self, cursor, size, *, fresh=False):
        self.calls.append(("discover", cursor, size))
        self.fresh_calls.append(fresh)
        value = self.pages[cursor]
        if isinstance(value, Exception):
            raise value
        return value

    async def artwork(self, value):
        self.calls.append(("artwork", value["id"]))
        return art_url(value["primary_image"]["image_id"])


class FakeNetwork:
    def __init__(self, root, *, payloads=None, dimensions=(1200, 1200), delays=None):
        self.root = root
        self.payloads = payloads or {}
        self.dimensions = dimensions
        self.delays = delays or {}
        self.calls = []
        self.completed = []
        self.max_bytes = 30 * 1024**2
        self.max_pixels = 40_000_000

    async def download(self, url):
        self.calls.append(url)
        await asyncio.sleep(self.delays.get(url, 0))
        content = self.payloads.get(url, url.encode())
        if isinstance(content, Exception):
            raise content
        digest = hashlib.sha256(content).hexdigest()
        path, preview = self.root / "covers" / f"{digest}.jpg", self.root / "thumbnails" / f"{digest}.jpg"
        path.write_bytes(content)
        preview.write_bytes(b"preview")
        self.completed.append(url)
        return Download(digest, path, preview, "JPEG", *self.dimensions, len(content), url)


@pytest.fixture
def state(tmp_path):
    value = State(tmp_path)
    yield value
    value.close()


def config(**changes):
    return {"source": "bandcamp", "mode": "vinyl", "quality": QUALITY, **changes}


def run(state, api, network, task, **changes):
    return asyncio.run(BandcampCollector(state, api, network).run(task["id"], **changes))


def stage(state, items=None, target=1):
    task = state.create_task(config(), target, 100)
    state.stage_bandcamp_page(task["id"], items or [sanitized()], None, "fixture exhausted")
    return task


def test_discovery_uses_observed_vinyl_payload_and_strips_unrelated_data():
    raw = item(featured_track={"stream_url": "do not store"}, following=True)
    raw["package_info"][0]["price"] = {"amount": 2000}
    network = MetadataNetwork(page_data([raw]))
    page = asyncio.run(BandcampAPI(network).discover("opaque", 25))
    assert network.calls == [(DISCOVERY_API, {"category_id": 2, "tag_norm_names": [],
        "geoname_id": 0, "slice": "top", "time_facet_id": None, "cursor": "opaque",
        "size": 25, "include_result_types": ["a", "s"], "followed_bands": False})]
    assert page.next_cursor == "next" and page.total == 100
    assert page.items[0]["id"] == "bandcamp:album:10:1"
    assert "featured_track" not in page.items[0] and "following" not in page.items[0]
    assert "price" not in page.items[0]["package_info"][0]


def test_discovery_refresh_is_explicit_and_keeps_default_cache_behavior():
    network = MetadataNetwork()
    api = BandcampAPI(network)
    asyncio.run(api.discover())
    asyncio.run(api.discover(fresh=True))
    assert network.fresh_calls == [False, True]
    assert network.calls[0] == network.calls[1]


@pytest.mark.parametrize("changes", [
    {"results": None}, {"results": [None]}, {"batch_result_count": 2},
    {"batch_result_count": True}, {"result_count": -1}, {"result_count": True},
    {"cursor": 123}, {"cursor": "*"},
    {"results": [item(band_id=True)]}, {"results": [item(item_type="../../album")]},
    {"results": [item(package_info=None)]},
])
def test_malformed_pages_do_not_supply_a_checkpoint(changes):
    network = MetadataNetwork(page_data(**changes))
    with pytest.raises(ApiDataError):
        asyncio.run(BandcampAPI(network).discover())


def test_ineligible_discovery_items_are_kept_in_original_order():
    items = [item(), item(2, item_type="t"), item(3, package_info=[])]
    page = asyncio.run(BandcampAPI(MetadataNetwork(page_data(items))).discover())
    assert [x["item_id"] for x in page.items] == [1, 2, 3]
    assert page.items[1]["id"] == "bandcamp:item-t:10:2"


def test_empty_discovery_page_ends_available_results_not_whole_archive():
    page = asyncio.run(BandcampAPI(MetadataNetwork(page_data([], cursor="ignored"))).discover())
    assert page.next_cursor is None
    assert "весь каталог не проверен" in page.exhausted_reason


def test_album_url_removes_tracking_without_modifying_album_identity():
    assert album_url("https://artist.bandcamp.com/album/test?from=discover#x") == "https://artist.bandcamp.com/album/test"


def test_album_url_accepts_bandcamp_artist_custom_domain_from_discover():
    assert album_url("https://listen.20buckspin.com/album/artifice-dissolves?from=discover_page") == "https://listen.20buckspin.com/album/artifice-dissolves"


@pytest.mark.parametrize("url", ["https://bandcamp.com.evil.test/album/test", "https://artist.bandcamp.com/track/test",
    "http://artist.bandcamp.com/album/test", "https://user:secret@artist.bandcamp.com/album/test",
    "file:///album/test", "https://artist.bandcamp.com:444/album/test",
    "https://localhost/album/test", "https://host.local/album/test", "https://host.internal/album/test",
    "https://127.0.0.1/album/test", "https://[::1]/album/test", "https://8.8.8.8/album/test",
    "https://2130706433/album/test", "https://0x7f000001/album/test", "https://0177.0.0.1/album/test",
    "https://artist.bandcamp.com/album/test name", "https://artist.bandcamp.com\\@localhost/album/test",
    "https://artist.bandcamp.com\n/album/test"])
def test_album_url_rejects_other_destinations(url):
    with pytest.raises(ApiDataError):
        album_url(url)


def test_artwork_uses_real_anchor_with_matching_identity_not_package_photo():
    html = f'<a class="popupImage" href="https://f4.bcbits.com/img/0001_10.jpg"><img src="package.jpg"></a>{art_html(597047764)}'
    assert parse_artwork(html, 597047764) == art_url(597047764)


@pytest.mark.parametrize("changes", [
    {"anchor": "https://f4.bcbits.com/img/a999_10.jpg"},
    {"anchor": "https://f4.bcbits.com/img/0001_10.jpg"},
    {"anchor": "http://f4.bcbits.com/img/a0000000001_10.jpg"},
    {"anchor": "https://bcbits.com.evil.test/img/a0000000001_10.jpg"},
    {"preview": "https://f4.bcbits.com/img/a999_2.jpg"},
])
def test_artwork_rejects_mismatched_unsafe_or_product_images(changes):
    with pytest.raises(ApiDataError):
        parse_artwork(art_html(**changes), 1)


def test_missing_main_artwork_never_falls_back_to_og_or_guesses_sizes():
    html = '<meta property="og:image" content="https://f4.bcbits.com/img/a0000000001_2.jpg">'
    with pytest.raises(FetchError, match="tralbumArt") as exc:
        parse_artwork(html, 1)
    assert exc.value.status == "no_front"


def test_api_loads_canonical_album_page_for_full_art_anchor():
    network = MetadataNetwork()
    result = asyncio.run(BandcampAPI(network).artwork(sanitized()))
    assert result == art_url()
    assert network.calls == ["https://label.bandcamp.com/album/album-1"]


def test_target_stages_whole_page_and_resume_keeps_seq_and_cursor(state):
    api = FakeAPI({"*": BandcampPage([sanitized(i) for i in range(1, 6)], "opaque-next", 100)})
    network = FakeNetwork(state.root)
    task = state.create_task(config(), 2, 100)
    run(state, api, network, task)
    before = [(r["seq"], r["entity_id"]) for r in state.rows(task["id"])]
    assert len(before) == 5 and state.count(task["id"]) == 2
    assert state.task(task["id"])["cursor"] == "opaque-next"
    assert state.bandcamp_cursors(task["id"]) == {"*"}
    run(state, api, network, task)
    assert len(network.calls) == 2
    state.create_task(config(), 4, 100)
    run(state, api, network, task)
    assert state.count(task["id"]) == len(network.calls) == 4
    assert [(r["seq"], r["entity_id"]) for r in state.rows(task["id"])] == before
    assert len([x for x in api.calls if x[0] == "discover"]) == 1


def test_opaque_pagination_follows_server_cursor_and_respects_candidate_budget(state):
    api = FakeAPI({"*": BandcampPage([sanitized(1)], "long opaque/==", 100),
        "long opaque/==": BandcampPage([sanitized(2)], "last", 100)})
    task = state.create_task(config(), 3, 2)
    network = FakeNetwork(state.root)
    reason = run(state, api, network, task)
    assert state.count(task["id"]) == 2 and "лимит просматриваемых кандидатов" in reason
    assert [x for x in api.calls if x[0] == "discover"] == [("discover", "*", 2), ("discover", "long opaque/==", 1)]


def test_failed_search_does_not_advance_cursor_or_mark_exhausted(state):
    api = FakeAPI({"*": FetchError("network_error", "offline")})
    task = state.create_task(config(), 1, 100)
    reason = run(state, api, FakeNetwork(state.root), task)
    assert "Checkpoint сохранён" in reason
    assert state.task(task["id"])["cursor"] == "*"
    assert not state.rows(task["id"]) and state.task(task["id"])["exhausted"] is None
    api.pages["*"] = BandcampPage([sanitized()], None, 1, "fixture exhausted")
    run(state, api, FakeNetwork(state.root), task)
    assert state.count(task["id"]) == 1


def test_cursor_cycle_is_rejected_across_separate_runs(state):
    task = state.create_task(config(), 1, 100)
    state.stage_bandcamp_page(task["id"], [sanitized()], "next")
    network = FakeNetwork(state.root)
    run(state, FakeAPI(), network, task)
    state.create_task(config(), 2, 100)
    api = FakeAPI({"next": BandcampPage([sanitized(2)], "*", 100)})
    reason = run(state, api, network, task)
    assert "цикл курсоров" in reason and "Checkpoint сохранён" in reason
    assert state.task(task["id"])["cursor"] == "next" and len(state.rows(task["id"])) == 1


@pytest.mark.parametrize("changes,status", [
    ({"item_type": "t"}, "not_album"), ({"package_info": [{"format": "cd", "package_id": 3}]}, "no_vinyl"),
    ({"primary_image": {"image_id": 1, "is_art": False}}, "no_front"),
    ({"primary_image": {"image_id": None, "is_art": True}}, "api_error"),
    ({"title": None}, "api_error"), ({"package_info": [{"format": "vinyl", "package_id": None}]}, "api_error"),
])
def test_ineligible_or_invalid_candidate_never_requests_artwork(state, changes, status):
    task = stage(state, [sanitized(**changes)])
    api, network = FakeAPI(), FakeNetwork(state.root)
    run(state, api, network, task)
    assert not api.calls and not network.calls
    assert state.rows(task["id"])[0]["status"] == status


def test_result_preserves_bandcamp_provenance_without_fake_musicbrainz_ids(state):
    task = stage(state)
    run(state, FakeAPI(), FakeNetwork(state.root), task)
    row = state.rows(task["id"])[0]
    assert row["source"] == "bandcamp" and row["source_entity_id"] == "bandcamp:album:10:1"
    assert row["artist"] == "Album artist" and row["title"] == "Album 1"
    assert row["source_image_id"] == "1" and row["vinyl_packages"][0]["format"] == "vinyl"
    assert row["artwork_type"] == "album_art" and "не подтверждённый скан" in row["selection_reason"]
    assert row["release_mbid"] is None and row["release_group_mbid"] is None and row["caa_image_id"] is None


def test_duplicates_keep_order_and_relationship_but_do_not_fill_unique_target(state):
    task = stage(state, [sanitized(i) for i in (1, 2, 3)], target=2)
    network = FakeNetwork(state.root, payloads={art_url(1): b"same", art_url(2): b"same"}, delays={art_url(1): .02})
    run(state, FakeAPI(), network, task)
    rows = state.rows(task["id"])
    assert [r["status"] for r in rows] == ["downloaded", "duplicate_image", "downloaded"]
    assert state.count(task["id"]) == 2
    assert network.completed[:2] == [art_url(2), art_url(1)]
    assert [r["bandcamp_item_id"] for r in rows] == [1, 2, 3]


@pytest.mark.parametrize("dimensions,review", [((1200,1200),False), ((500,500),True), ((1200,800),True)])
def test_review_flags_only_image_dimensions(state, dimensions, review):
    task = stage(state)
    run(state, FakeAPI(), FakeNetwork(state.root, dimensions=dimensions), task)
    assert state.rows(task["id"])[0]["needs_review"] is review


def test_retry_does_not_discover_new_items(state):
    task = stage(state)
    seq = state.rows(task["id"])[0]["seq"]
    state.failed(seq, "network_error", "fixture")
    state.retry(task["id"])
    api = FakeAPI()
    run(state, api, FakeNetwork(state.root), task, discover=False)
    assert state.count(task["id"]) == 1
    assert not any(x[0] == "discover" for x in api.calls)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "thumbnail"])
def test_network_preserves_original_bytes_and_repairs_exact_source_same_entry(state, damage):
    content = BytesIO()
    Image.new("RGB", (800, 800), "teal").save(content, "JPEG")
    body, calls = content.getvalue(), []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=body, headers={"Content-Type":"image/jpeg"})
    task, api = stage(state), FakeAPI()
    async def collect_once():
        async with Network("https://example.invalid/coverpacks-tests", state.root, transport=httpx.MockTransport(handler)) as network:
            await BandcampCollector(state, api, network).run(task["id"])
    asyncio.run(collect_once())
    before = state.rows(task["id"])[0]
    original = state.root / before["path"]
    assert original.read_bytes() == body
    if damage == "missing":
        original.unlink()
    elif damage == "corrupt":
        original.write_bytes(b"corrupt")
    else:
        (state.root / before["thumbnail_path"]).unlink()
    asyncio.run(collect_once())
    after = state.rows(task["id"])[0]
    assert after["seq"] == before["seq"] and after["sha256"] == before["sha256"]
    assert after["attempts"] == 2 and original.read_bytes() == body
    assert len(api.calls) == 1  # repair never reselects art from an updated page
    assert calls == [art_url()] * (1 if damage == "thumbnail" else 2)


def test_changed_source_during_repair_flags_review_and_keeps_previous_hash(state):
    task, api, network = stage(state), FakeAPI(), FakeNetwork(state.root)
    run(state, api, network, task)
    before = state.rows(task["id"])[0]
    (state.root / before["path"]).unlink()
    network.payloads[art_url()] = b"upstream changed"
    run(state, api, network, task)
    after = state.rows(task["id"])[0]
    assert after["status"] == "needs_review" and after["previous_sha256"] == before["sha256"]
    assert after["seq"] == before["seq"] and len(api.calls) == 1


def test_artwork_reuses_cached_source_between_tasks(state):
    first = stage(state)
    network = FakeNetwork(state.root)
    run(state, FakeAPI(), network, first)
    second = state.create_task(config(marker="second"), 1, 100)
    state.stage_bandcamp_page(second["id"], [sanitized()], None, "fixture exhausted")
    run(state, FakeAPI(), network, second)
    assert network.calls == [art_url()]
    assert state.count(first["id"]) == state.count(second["id"]) == 1


def test_head_refresh_preserves_old_queue_snapshots_and_originals_when_rank_changes(state):
    task = state.create_task(config(), 1, 120)
    state.stage_bandcamp_page(task["id"], [sanitized(1), sanitized(2)], "old-tail")
    network = FakeNetwork(state.root)
    run(state, FakeAPI(), network, task)
    first = state.rows(task["id"])[0]
    state.exclude(first["seq"])
    state.set_task(task["id"], exhausted="previous scan exhausted", stop_reason="previous stop")
    before_task = state.task(task["id"])
    before_entries = [tuple(row) for row in state.db.execute("SELECT * FROM entries ORDER BY seq")]
    source = state.root / first["path"]
    before_file = (source.read_bytes(), source.stat().st_mtime_ns)
    api = FakeAPI({
        "*": BandcampPage([sanitized(2), sanitized(3), sanitized(1, title="Changed upstream")], "old-tail", 100),
        "old-tail": BandcampPage([sanitized(4)], None, 100),
    })
    collector = BandcampCollector(state, api, network)
    report = asyncio.run(collector.refresh_head(task["id"], max_pages=2))
    assert report == {"pages": 2, "examined": 4, "added": 2, "known": 2, "stopped": "end_of_results"}
    assert state.task(task["id"]) == before_task
    assert state.bandcamp_cursors(task["id"]) == {"*"}
    assert [tuple(row) for row in state.db.execute("SELECT * FROM entries ORDER BY seq LIMIT 2")] == before_entries
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before_file
    assert [row["entity_id"] for row in state.rows(task["id"])] == [f"bandcamp:album:10:{i}" for i in (1, 2, 3, 4)]
    assert [row["status"] for row in state.rows(task["id"])] == ["downloaded", "pending", "pending", "pending"]
    repeated = asyncio.run(collector.refresh_head(task["id"], max_pages=2))
    assert repeated == {"pages": 2, "examined": 4, "added": 0, "known": 4, "stopped": "end_of_results"}
    assert state.task(task["id"]) == before_task and len(state.rows(task["id"])) == 4
    assert api.fresh_calls == [True] * 4
    assert network.calls == [art_url(1)]  # refresh itself never downloads artwork


def test_head_refresh_passes_known_pages_but_respects_its_page_budget(state):
    task = state.create_task(config(), 1, 1)
    state.stage_bandcamp_page(task["id"], [sanitized(1)], "saved-tail")
    api = FakeAPI({
        "*": BandcampPage([sanitized(1)], "second", 100),
        "second": BandcampPage([sanitized(2)], "third", 100),
        "third": BandcampPage([sanitized(3)], None, 100),
    })
    report = asyncio.run(BandcampCollector(state, api, FakeNetwork(state.root)).refresh_head(task["id"], max_pages=2))
    assert report == {"pages": 2, "examined": 2, "added": 1, "known": 1, "stopped": "page_limit"}
    assert [call[1] for call in api.calls] == ["*", "second"]
    assert state.task(task["id"])["cursor"] == "saved-tail"
    assert state.task(task["id"])["discovered"] == 1


def test_head_refresh_committed_page_survives_interruption_and_reopen(tmp_path):
    state = State(tmp_path)
    task = state.create_task(config(), 1, 50)
    state.stage_bandcamp_page(task["id"], [sanitized(1)], "saved-tail")
    before_task = state.task(task["id"])
    api = FakeAPI({"*": BandcampPage([sanitized(2)], "second", 100),
                   "second": FetchError("network_error", "interrupted")})
    try:
        with pytest.raises(FetchError, match="interrupted"):
            asyncio.run(BandcampCollector(state, api, FakeNetwork(state.root)).refresh_head(task["id"]))
    finally:
        state.close()
    resumed = State(tmp_path)
    try:
        assert resumed.task(task["id"]) == before_task
        before_seq = [(row["seq"], row["entity_id"]) for row in resumed.rows(task["id"])]
        assert len(before_seq) == 2
        api.pages["second"] = BandcampPage([sanitized(3)], None, 100)
        report = asyncio.run(BandcampCollector(resumed, api, FakeNetwork(resumed.root)).refresh_head(task["id"]))
        assert report["added"] == 1 and report["known"] == 1
        assert [(row["seq"], row["entity_id"]) for row in resumed.rows(task["id"])][:2] == before_seq
        assert resumed.task(task["id"]) == before_task
    finally:
        resumed.close()


def test_head_refresh_cursor_cycles_reject_page_without_poisoning_next_pass(state):
    task = state.create_task(config())
    api = FakeAPI({"*": BandcampPage([sanitized(1)], "second", 100),
                   "second": BandcampPage([sanitized(2)], "*", 100)})
    collector = BandcampCollector(state, api, FakeNetwork(state.root))
    with pytest.raises(ApiDataError, match="цикл курсоров"):
        asyncio.run(collector.refresh_head(task["id"]))
    assert len(state.rows(task["id"])) == 1
    assert state.bandcamp_cursors(task["id"]) == set()
    api.pages["second"] = BandcampPage([sanitized(2)], None, 100)
    assert asyncio.run(collector.refresh_head(task["id"]))["added"] == 1


@pytest.mark.parametrize("maximum", [0, -1, True, 1.5, None])
def test_head_refresh_rejects_invalid_budget_before_requests(state, maximum):
    task = state.create_task(config())
    api = FakeAPI()
    with pytest.raises(ValueError, match="max_pages"):
        asyncio.run(BandcampCollector(state, api, FakeNetwork(state.root)).refresh_head(task["id"], max_pages=maximum))
    assert api.calls == [] and state.rows(task["id"]) == []


def test_head_refresh_rejects_other_sources_before_requests(state):
    task = state.create_task({"mode": "vinyl"})
    api = FakeAPI()
    with pytest.raises(ValueError, match="Bandcamp"):
        asyncio.run(BandcampCollector(state, api, FakeNetwork(state.root)).refresh_head(task["id"]))
    assert api.calls == []


def test_repairs_find_late_head_entries_without_normal_discovery_count(state):
    task = state.create_task(config(), 1, 10)
    state.stage_bandcamp_head_page(task["id"], [sanitized(i) for i in range(1, 6)])
    network = FakeNetwork(state.root)
    last = state.queue(task["id"], 5)[-1]
    initial = asyncio.run(BandcampCollector(state, FakeAPI(), network).process(last, task["config"]))
    state.success(last["seq"], initial)
    (state.root / initial["path"]).unlink()
    assert state.task(task["id"])["discovered"] == 0
    run(state, FakeAPI(), network, task, discover=False)
    assert [row["status"] for row in state.rows(task["id"])] == ["pending"] * 4 + ["downloaded"]
    assert network.calls == [art_url(5), art_url(5)]
    assert state.count(task["id"]) == 1


def test_collector_does_not_redownload_verified_archived_original(state):
    task = stage(state)
    network = FakeNetwork(state.root)
    run(state, FakeAPI(), network, task)
    original = state.rows(task["id"])[0]
    (state.root / original["path"]).unlink()
    (state.root / original["thumbnail_path"]).unlink()
    api = FakeAPI()
    run(state, api, network, task, archived_hashes=frozenset({original["sha256"]}))
    assert state.rows(task["id"])[0] == original
    assert network.calls == [art_url()] and api.calls == []


def test_new_head_id_with_existing_image_hash_does_not_add_a_unique_cover(state):
    task = stage(state)
    network = FakeNetwork(state.root, payloads={art_url(1): b"same artwork", art_url(2): b"same artwork"})
    run(state, FakeAPI(), network, task)
    first = state.rows(task["id"])[0]
    api = FakeAPI({"*": BandcampPage([sanitized(2), sanitized(1)], None, 100)})
    collector = BandcampCollector(state, api, network)
    assert asyncio.run(collector.refresh_head(task["id"]))["added"] == 1
    state.set_task(task["id"], target=2)
    run(state, FakeAPI(), network, task, discover=False)
    rows = state.rows(task["id"])
    assert [row["status"] for row in rows] == ["downloaded", "duplicate_image"]
    assert rows[0] == first and rows[1]["sha256"] == first["sha256"]
    assert state.count(task["id"]) == 1


@pytest.mark.parametrize("third_error", [None, "blocked"])
def test_album_rate_limit_commits_inflight_batch_keeps_pending_and_resumes(state, monkeypatch, third_error):
    monkeypatch.setattr("coverpacks.bandcamp.time.time", lambda: 1000.0)
    class LimitedArtworkAPI(FakeAPI):
        def __init__(self):
            super().__init__()
            self.errors = {1: FetchError("rate_limited", "Bandcamp HTTP 429", retry_after=75)}
            if third_error:
                self.errors[3] = FetchError(third_error, "Bandcamp HTTP 403")

        async def artwork(self, value):
            url = await super().artwork(value)
            if error := self.errors.get(value["item_id"]):
                raise error
            return url

    task = stage(state, [sanitized(i) for i in range(1, 6)], target=5)
    api = LimitedArtworkAPI()
    network = FakeNetwork(state.root, delays={art_url(2): .01})
    identities = [(row["seq"], row["entity_id"]) for row in state.rows(task["id"])]
    collector = BandcampCollector(state, api, network)
    reason = asyncio.run(collector.run(task["id"]))
    first = state.rows(task["id"])
    assert [row["status"] for row in first] == ["pending", "downloaded", third_error or "downloaded", "pending", "pending"]
    assert [row["attempts"] for row in first] == [1, 1, 1, 0, 0]
    assert first[0]["last_error"] == "Bandcamp HTTP 429"
    assert "rate_limited" in reason and "Retry-After=75" in reason
    assert collector.rate_limited_until == 1075.0
    assert state.task(task["id"])["stop_reason"] == reason
    assert [call[1] for call in api.calls] == [f"bandcamp:album:10:{i}" for i in (1, 2, 3)]
    assert set(network.completed) == ({art_url(2)} if third_error else {art_url(2), art_url(3)})

    # A later pass naturally retries pending entries; no explicit retry/reset is needed.
    api.errors.pop(1)
    asyncio.run(collector.run(task["id"]))
    assert collector.rate_limited_until is None
    resumed = state.rows(task["id"])
    assert [row["status"] for row in resumed] == ["downloaded", "downloaded", third_error or "downloaded", "downloaded", "downloaded"]
    assert [row["attempts"] for row in resumed] == [2, 1, 1, 1, 1]
    assert [(row["seq"], row["entity_id"]) for row in resumed] == identities
    assert resumed[1] == first[1]
    assert network.calls.count(art_url(2)) == 1
    assert state.count(task["id"]) == (4 if third_error else 5)
    calls = list(api.calls), list(network.calls)
    run(state, api, network, task)
    assert (api.calls, network.calls) == calls


@pytest.mark.parametrize("retry_after", [None, 25.0])
def test_process_preserves_rate_limit_status_and_optional_delay(state, retry_after):
    task = stage(state)

    class LimitedAPI(FakeAPI):
        async def artwork(self, value):
            raise FetchError("rate_limited", "Bandcamp HTTP 429", retry_after=retry_after)

    network = FakeNetwork(state.root)
    result = asyncio.run(BandcampCollector(state, LimitedAPI(), network).process(
        state.queue(task["id"], 1)[0], task["config"]))
    expected = {"error_status": "rate_limited", "error": "Bandcamp HTTP 429"}
    if retry_after is not None:
        expected["retry_after"] = retry_after
    assert result == expected and network.calls == []


@pytest.mark.parametrize("delays,expected", [([20, 120], 1120.0), ([20, None], 1060.0)])
def test_batch_rate_limit_deadline_respects_longest_response_and_default(state, monkeypatch, delays, expected):
    monkeypatch.setattr("coverpacks.bandcamp.time.time", lambda: 1000.0)
    task = stage(state, [sanitized(1), sanitized(2)], target=2)

    class LimitedAPI(FakeAPI):
        async def artwork(self, value):
            raise FetchError("rate_limited", "Bandcamp HTTP 429", retry_after=delays[value["item_id"] - 1])

    collector = BandcampCollector(state, LimitedAPI(), FakeNetwork(state.root))
    asyncio.run(collector.run(task["id"]))
    assert collector.rate_limited_until == expected
    assert [row["status"] for row in state.rows(task["id"])] == ["pending", "pending"]
    assert [row["attempts"] for row in state.rows(task["id"])] == [1, 1]


def test_discovery_rate_limit_preserves_checkpoint_and_next_pass_resumes(state, monkeypatch):
    monkeypatch.setattr("coverpacks.bandcamp.time.time", lambda: 1000.0)
    task = state.create_task(config(), 1, 100)
    api = FakeAPI({"*": FetchError("rate_limited", "Discover HTTP 429", retry_after=40)})
    network = FakeNetwork(state.root)
    collector = BandcampCollector(state, api, network)
    reason = asyncio.run(collector.run(task["id"]))
    assert "rate_limited" in reason and "Retry-After=40" in reason
    assert collector.rate_limited_until == 1040.0
    assert state.rows(task["id"]) == []
    assert state.task(task["id"])["cursor"] == "*"
    assert state.task(task["id"])["exhausted"] is None
    assert state.bandcamp_cursors(task["id"]) == set()
    assert api.calls == [("discover", "*", 60)] and network.calls == []

    api.pages["*"] = BandcampPage([sanitized(1)], None, 1, "fixture exhausted")
    asyncio.run(collector.run(task["id"]))
    assert collector.rate_limited_until is None
    assert state.count(task["id"]) == 1
    assert len(state.rows(task["id"])) == 1
    assert state.rows(task["id"])[0]["attempts"] == 1


def test_head_rate_limit_preserves_staged_pages_and_stops_before_more_requests(state, monkeypatch):
    monkeypatch.setattr("coverpacks.bandcamp.time.time", lambda: 1000.0)
    task = state.create_task(config(), 1, 100)
    state.stage_bandcamp_page(task["id"], [sanitized(1)], "saved-tail")
    before_task = state.task(task["id"])
    api = FakeAPI({
        "*": BandcampPage([sanitized(1), sanitized(2)], "second", 100),
        "second": FetchError("rate_limited", "Discover HTTP 429", retry_after=90),
    })
    network = FakeNetwork(state.root)
    collector = BandcampCollector(state, api, network)
    report = asyncio.run(collector.refresh_head(task["id"], max_pages=4))
    assert report == {"pages": 1, "examined": 2, "added": 1, "known": 1,
                      "stopped": "rate_limited", "error": "Discover HTTP 429", "retry_after": 90,
                      "not_before": 1090.0}
    assert collector.rate_limited_until == 1090.0
    assert [call[1] for call in api.calls] == ["*", "second"]
    assert api.fresh_calls == [True, True]
    assert network.calls == [] and state.task(task["id"]) == before_task
    assert state.bandcamp_cursors(task["id"]) == {"*"}
    identities = [(row["seq"], row["entity_id"]) for row in state.rows(task["id"])]
    assert [row["status"] for row in state.rows(task["id"])] == ["pending", "pending"]

    api.pages["second"] = BandcampPage([sanitized(3)], None, 100)
    resumed = asyncio.run(collector.refresh_head(task["id"]))
    assert collector.rate_limited_until is None
    assert resumed == {"pages": 2, "examined": 3, "added": 1, "known": 2, "stopped": "end_of_results"}
    assert [(row["seq"], row["entity_id"]) for row in state.rows(task["id"])][:2] == identities
    assert len(state.rows(task["id"])) == 3
    assert state.task(task["id"]) == before_task


def test_head_rate_limit_on_first_request_defaults_to_60_second_delay(state, monkeypatch):
    monkeypatch.setattr("coverpacks.bandcamp.time.time", lambda: 1000.0)
    task = state.create_task(config())
    api = FakeAPI({"*": FetchError("rate_limited", "Discover HTTP 429")})
    collector = BandcampCollector(state, api, FakeNetwork(state.root))
    report = asyncio.run(collector.refresh_head(task["id"]))
    assert report == {"pages": 0, "examined": 0, "added": 0, "known": 0,
                      "stopped": "rate_limited", "error": "Discover HTTP 429", "not_before": 1060.0}
    assert collector.rate_limited_until == 1060.0
    assert state.rows(task["id"]) == [] and api.calls == [("discover", "*", 60)]

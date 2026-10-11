"""Offline fixtures exercise API contracts; these are not live API responses."""

import asyncio
from collections import deque

import pytest

from coverpacks.api import (
    ApiDataError, ArtistAmbiguous, CatalogAPI, MAX_FRONT_IMAGES,
    VINYL_FORMATS, escape_lucene, is_album_group, is_vinyl_release, mbid,
)
from coverpacks.network import FetchError


RG = "00000000-0000-4000-8000-000000000001"
R1 = "00000000-0000-4000-8000-000000000002"
R2 = "00000000-0000-4000-8000-000000000003"
R3 = "00000000-0000-4000-8000-000000000004"
ARTIST = "00000000-0000-4000-8000-000000000005"


class FakeNetwork:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    async def json(self, url, params=None, musicbrainz=False):
        self.calls.append((url, params, musicbrainz))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def run(awaitable):
    return asyncio.run(awaitable)


def release(rid, format='12" Vinyl', date="2000"):
    return {"id": rid, "release-group": {"id": RG, "primary-type": "Album"},
            "media": [{"format": format}], "date": date}


def cover(image_id="123", front=True, approved=True, **extra):
    return {"id": image_id, "front": front, "approved": approved,
            "image": f"https://coverartarchive.org/release/{R1}/{image_id}.png",
            "thumbnails": {size: f"https://coverartarchive.org/release/{R1}/{image_id}-{size}.jpg"
                           for size in ("1200", "500", "250")}, **extra}


def test_search_uses_response_length_not_requested_limit():
    net = FakeNetwork({"count": 50, "offset": 0, "release-groups": [{"id": RG}, {"id": R1}]})
    page = run(CatalogAPI(net).search_page("albums", limit=100))
    assert page.next_offset == 2
    assert page.total == 50
    assert net.calls[0][1] == {"query": "primarytype:album", "offset": 0, "limit": 100, "fmt": "json"}
    assert net.calls[0][2] is True


def test_search_mode_formats_artist_and_no_taste_filters():
    net = FakeNetwork({"count": 0, "offset": 0, "releases": []})
    page = run(CatalogAPI(net).search_page("vinyl", ARTIST))
    url, params, _ = net.calls[0]
    assert url.endswith("/release")
    for term in ("vinyl", "7vinyl", "10vinyl", "12vinyl", "flexidisc", "7flexidisc"):
        assert f"format:{term}" in params["query"]
    assert f"arid:{ARTIST}" in params["query"]
    assert "primarytype:album" in params["query"]
    assert "status:" not in params["query"]
    assert page.exhausted_reason == "search_results_exhausted"


def test_search_window_is_explicit_and_clamps_page_limit():
    net = FakeNetwork({"count": 90, "offset": 2, "release-groups": [{"id": RG}]})
    page = run(CatalogAPI(net, search_window=3).search_page("albums", offset=2))
    assert net.calls[0][1]["limit"] == 1
    assert page.next_offset is None
    assert page.exhausted_reason == "search_window"
    assert page.total == 90
    net = FakeNetwork()
    page = run(CatalogAPI(net, search_window=3).search_page("albums", offset=3))
    assert not net.calls
    assert page.exhausted_reason == "search_window"


def test_empty_search_page_is_not_claimed_as_entire_archive():
    net = FakeNetwork({"count": 90, "offset": 2, "releases": []})
    page = run(CatalogAPI(net).search_page("vinyl", offset=2))
    assert page.exhausted_reason == "empty_search_page"


@pytest.mark.parametrize("data", [
    {"count": 20, "offset": 1, "releases": []},
    {"count": 20, "offset": 0},
    {"count": "20", "offset": 0, "releases": []},
])
def test_malformed_page_does_not_masquerade_as_no_results(data):
    with pytest.raises(ApiDataError):
        run(CatalogAPI(FakeNetwork(data)).search_page("vinyl"))


@pytest.mark.parametrize("item", [{}, {"id": None}, {"id": "bad-mbid"},
                                  {"id": RG, "release-group": {}}])
def test_invalid_entity_id_stops_page_before_staging(item):
    net = FakeNetwork({"count": 1, "offset": 0, "releases": [item]})
    with pytest.raises(ApiDataError):
        run(CatalogAPI(net).search_page("vinyl"))


def test_search_entity_and_nested_group_ids_are_canonical():
    uppercase = "A0000000-ABCD-4000-8000-000000000001"
    net = FakeNetwork({"count": 1, "offset": 0, "releases": [
        {"id": uppercase, "release-group": {"id": uppercase}},
    ]})
    page = run(CatalogAPI(net).search_page("vinyl"))
    assert page.items[0]["id"] == uppercase.lower()
    assert page.items[0]["release-group"]["id"] == uppercase.lower()


@pytest.mark.parametrize("format", sorted(VINYL_FORMATS))
def test_all_documented_vinyl_formats(format):
    assert is_vinyl_release(release(R1, format))


@pytest.mark.parametrize("format", ["CD", "Digital Media", "Shellac", '12" Shellac',
                                    "Acetate", "Phonograph record", "VinylDisc", "Cassette", None])
def test_physical_or_unknown_does_not_mean_vinyl(format):
    assert not is_vinyl_release(release(R1, format))


def test_bundle_with_confirmed_vinyl_medium_is_a_vinyl_edition():
    assert is_vinyl_release({"media": [{"format": "CD"}, {"format": '7" Vinyl'}]})
    assert not is_vinyl_release({"title": "Vinyl", "media": []})
    assert not is_vinyl_release({})
    assert not is_vinyl_release({"media": [{"format": ["CD", "Vinyl"]}]})
    assert is_album_group({"primary-type": "Album", "secondary-types": ["Live"]})
    assert not is_album_group({"primary-type": "Single"})


def test_browse_short_page_keeps_remaining_releases_and_sort_is_stable():
    net = FakeNetwork(
        {"release-count": 8, "release-offset": 0, "releases": [release(R1, date="2020")]},
        {"release-count": 8, "release-offset": 1, "releases": [release(R2, date="1999"), release(R3, date=None)]},
    )
    items = run(CatalogAPI(net).releases(RG, cap=3))
    assert [item["id"] for item in items] == [R2, R1, R3]
    assert [call[1]["offset"] for call in net.calls] == [0, 1]
    assert [call[1]["limit"] for call in net.calls] == [3, 2]
    assert all("query" not in call[1] for call in net.calls)


def test_vinyl_fallback_verifies_lookup_and_cap_includes_nonvinyl_hits():
    net = FakeNetwork(
        {"count": 20, "offset": 0, "releases": [{"id": R1}, {"id": R2}]},
        release(R1, format="CD"),
        release(R2, format='10" Vinyl'),
    )
    items = run(CatalogAPI(net).releases(RG, cap=2, vinyl=True))
    assert [item["id"] for item in items] == [R2]
    assert len(net.calls) == 3
    assert f"rgid:{RG}" in net.calls[0][1]["query"]
    assert all("media" in call[1]["inc"] for call in net.calls[1:])


def test_vinyl_other_group_stale_search_hit_is_not_used():
    wrong = release(R1)
    wrong["release-group"]["id"] = R3
    net = FakeNetwork({"count": 1, "offset": 0, "releases": [{"id": R1}]}, wrong)
    assert run(CatalogAPI(net).releases(RG, cap=2, vinyl=True)) == []


def test_stale_404_does_not_discard_prior_release_or_exceed_candidate_cap():
    net = FakeNetwork(
        {"count": 20, "offset": 0, "releases": [{"id": R1}, {"id": R2}]},
        release(R1), FetchError("not_found", "Release removed"),
    )
    items = run(CatalogAPI(net).releases(RG, cap=2, vinyl=True))
    assert [item["id"] for item in items] == [R1]
    assert len(net.calls) == 3


def test_stale_404_continues_to_later_release_in_same_page():
    net = FakeNetwork(
        {"count": 2, "offset": 0, "releases": [{"id": R1}, {"id": R2}]},
        FetchError("not_found", "Release removed"), release(R2),
    )
    assert [i["id"] for i in run(CatalogAPI(net).releases(RG, cap=2, vinyl=True))] == [R2]


@pytest.mark.parametrize("status", ["network_error", "blocked", "invalid_json"])
def test_release_lookup_errors_other_than_404_remain_errors(status):
    error = FetchError(status, "Temporary or invalid response")
    net = FakeNetwork({"count": 1, "offset": 0, "releases": [{"id": R1}]}, error)
    with pytest.raises(FetchError) as caught:
        run(CatalogAPI(net).releases(RG, cap=2, vinyl=True))
    assert caught.value is error


def test_release_lookup_validates_nested_group_id():
    data = release(R1)
    data["release-group"] = {"id": "bad-id"}
    with pytest.raises(ApiDataError):
        run(CatalogAPI(FakeNetwork(data)).release(R1))


def test_artist_homonyms_are_not_ranked_away():
    net = FakeNetwork({"count": 2, "offset": 0, "artists": [
        {"id": R2, "name": "Alice", "score": 100, "disambiguation": "British"},
        {"id": ARTIST, "name": "Alice", "score": 85, "disambiguation": "French"},
    ]})
    with pytest.raises(ArtistAmbiguous) as caught:
        run(CatalogAPI(net).resolve_artist("Alice"))
    assert len(caught.value.options) == 2
    assert caught.value.total == 2


def test_artist_ambiguity_counts_not_only_returned_page():
    net = FakeNetwork({"count": 101, "offset": 0, "artists": [{"id": ARTIST, "name": "Alice"}]})
    with pytest.raises(ArtistAmbiguous):
        run(CatalogAPI(net).resolve_artist("Alice"))


def test_unique_artist_requires_literal_name_and_query_is_escaped():
    name = 'AC/DC + "Friends"'
    net = FakeNetwork({"count": 1, "offset": 0, "artists": [{"id": ARTIST, "name": name}]})
    assert run(CatalogAPI(net).resolve_artist(name))["id"] == ARTIST
    assert net.calls[0][1]["query"] == 'artist:"AC\\/DC \\+ \\"Friends\\""'
    assert escape_lucene("a:(b)*?") == r"a\:\(b\)\*\?"
    net = FakeNetwork({"count": 1, "offset": 0, "artists": [{"id": ARTIST, "name": "Alice Cooper"}]})
    with pytest.raises(ArtistAmbiguous):
        run(CatalogAPI(net).resolve_artist("Alice"))


def test_no_front_means_no_cover_even_when_other_image_is_first():
    net = FakeNetwork({"images": [cover(front=False, types=["Back"])]})
    assert run(CatalogAPI(net).cover_options("release", R1)) == []
    # A Front type is not the documented main-front flag.
    net = FakeNetwork({"images": [cover(front=False, types=["Front"])]})
    assert run(CatalogAPI(net).cover_options("release", R1)) == []


def test_front_selection_provenance_and_thumbnail_fallback_order():
    net = FakeNetwork({"release": f"https://musicbrainz.org/release/{R1}", "images": [
        cover("1", approved=False), cover("10"), cover("2"), cover("0", front=False), cover("11"),
    ]})
    options = run(CatalogAPI(net).cover_options("release-group", RG))
    assert [i.image_id for i in options[::4]] == ["2", "10", "11"]
    assert len(options) == MAX_FRONT_IMAGES * 4
    assert [i.variant for i in options[:4]] == ["1200", "500", "250", "original"]
    assert all(i.release_id == R1 and i.front is True for i in options)
    assert all(i.metadata_url.endswith(f"/release-group/{RG}") for i in options)
    assert options[0].image_url.endswith("2.png")
    assert "fallback_from_1200" in options[1].reason


def test_original_quality_uses_image_and_does_not_label_thumbnail_original():
    net = FakeNetwork({"images": [cover()]})
    options = run(CatalogAPI(net).cover_options("release", R1, "original"))
    assert [i.variant for i in options] == ["original", "1200", "500", "250"]
    assert options[0].url == options[0].image_url
    assert options[1].url.endswith("-1200.jpg")
    assert "fallback_from_original" in options[1].reason


def test_missing_version_fallback_uses_returned_urls_and_legacy_aliases():
    net = FakeNetwork({"images": [cover(thumbnails={"large": "https://example.org/large.jpg",
                                                       "small": "https://example.org/small.jpg"}, approved=None)]})
    options = run(CatalogAPI(net).cover_options("release", R1))
    assert [i.variant for i in options] == ["500", "250", "original"]
    assert options[0].url == "https://example.org/large.jpg"
    assert options[0].approved is None
    assert options[0].requested_quality == "1200"


def test_caa_http_error_is_not_converted_to_missing_front():
    error = RuntimeError("503 temporary failure")
    with pytest.raises(RuntimeError) as caught:
        run(CatalogAPI(FakeNetwork(error)).cover_options("release-group", RG))
    assert caught.value is error


def test_caa_malformed_and_wrong_release_metadata_rejected():
    for data in ({"error": "something"}, {"images": [cover(image_id=None)]},
                 {"images": [cover()], "release": f"https://musicbrainz.org/release/{R2}"}):
        with pytest.raises(ApiDataError):
            run(CatalogAPI(FakeNetwork(data)).cover_options("release", R1))


def test_mbid_validation_precedes_url_construction():
    assert mbid(ARTIST) == ARTIST
    with pytest.raises(ValueError):
        run(CatalogAPI(FakeNetwork()).release("../../secret"))


def test_group_lookup_does_not_truncate_editions_via_inc_releases():
    net = FakeNetwork({"id": RG, "primary-type": "Album"})
    run(CatalogAPI(net).group(RG))
    assert net.calls[0][1]["inc"] == "artist-credits"

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from coverpacks import network
from coverpacks.network import FetchError, Network


def image_bytes(format="PNG", size=(40, 25)):
    buffer = BytesIO()
    Image.new("RGB", size, (30, 70, 140)).save(buffer, format=format)
    return buffer.getvalue()


def client(tmp_path, handler, **kwargs):
    return Network("https://example.test/coverpacks", tmp_path, transport=httpx.MockTransport(handler), **kwargs)


def test_redirect_actual_format_original_bytes_and_repair(tmp_path):
    original = image_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/front.jpg":
            return httpx.Response(307, headers={"location": "https://images.example.test/actual"})
        return httpx.Response(200, content=original, headers={"content-type": "image/jpeg"})

    async def scenario():
        async with client(tmp_path, handler) as http:
            first = await http.download("https://caa.example.test/front.jpg")
            assert first.path.read_bytes() == original
            assert first.format == "PNG" and first.path.suffix == ".png"
            assert (first.width, first.height, first.size_bytes) == (40, 25, len(original))
            assert first.sha256 == hashlib.sha256(original).hexdigest()
            assert first.url == "https://images.example.test/actual"
            with Image.open(first.thumbnail_path) as preview:
                assert preview.format == "JPEG"
                assert preview.size == (40, 25)
            first.path.write_bytes(b"broken local file")
            second = await http.download("https://caa.example.test/front.jpg")
            assert second.path == first.path
            assert second.path.read_bytes() == original
            first.path.unlink()
            third = await http.download("https://caa.example.test/front.jpg")
            assert third.path == first.path and third.path.read_bytes() == original

    asyncio.run(scenario())
    assert len(list((tmp_path / "covers").iterdir())) == 1
    assert len(list((tmp_path / "thumbnails").iterdir())) == 1
    assert len(requests) == 6
    assert requests[0].headers["user-agent"] == "coverpacks/0.1.0 (https://example.test/coverpacks)"


@pytest.mark.parametrize(("code", "status"), [(404, "not_found"), (410, "not_found"), (403, "blocked"), (451, "blocked"), (400, "http_error")])
def test_permanent_http_errors_do_not_retry(tmp_path, code, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code)

    async def scenario():
        async with client(tmp_path, handler) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/image")
            assert error.value.status == status

    asyncio.run(scenario())
    assert len(calls) == 1
    assert not list(tmp_path.rglob("*.part"))


def test_retry_429_503_respects_retry_after(tmp_path, monkeypatch):
    waits = []
    calls = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(network.asyncio, "sleep", sleep)
    monkeypatch.setattr(network.random, "uniform", lambda *_: 0)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=image_bytes())

    async def scenario():
        async with client(tmp_path, handler) as http:
            await http.download("https://example.test/image")

    asyncio.run(scenario())
    assert waits == [2, 1]
    assert len(calls) == 3


def test_retry_limit_and_long_retry_after(tmp_path, monkeypatch):
    waits = []
    calls = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(network.asyncio, "sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "10000"} if request.url.path == "/later" else {})

    async def scenario():
        async with client(tmp_path, handler, retries=1) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/limited")
            assert error.value.status == "network_error"
            assert len(calls) == 2 and len(waits) == 1
            with pytest.raises(FetchError, match="Retry-After"):
                await http.download("https://example.test/later")
            assert len(calls) == 3 and len(waits) == 1

    asyncio.run(scenario())


def test_retry_after_http_date():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 28 <= network._retry_after(future) <= 31
    assert network._retry_after("invalid") == 0
    assert network._retry_after("-1") == 0


@pytest.mark.parametrize("body", [b"<!DOCTYPE html><html>blocked</html>", b"not an image", image_bytes()[:40]])
def test_html_and_corrupt_images_are_not_saved(tmp_path, body):
    async def scenario():
        async with client(tmp_path, lambda _: httpx.Response(200, content=body)) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/image")
            assert error.value.status == "invalid_image"

    asyncio.run(scenario())
    assert not list((tmp_path / "covers").iterdir())
    assert not list((tmp_path / "thumbnails").iterdir())


def test_decoding_detects_truncated_jpeg(tmp_path):
    original = image_bytes("JPEG", size=(100, 100))

    async def scenario():
        async with client(tmp_path, lambda _: httpx.Response(200, content=original[:-40])) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/image")
            assert error.value.status == "invalid_image"

    asyncio.run(scenario())
    assert not list(tmp_path.rglob("*.part"))


def test_pixel_limit(tmp_path):
    async def scenario():
        async with client(tmp_path, lambda _: httpx.Response(200, content=image_bytes(size=(100, 100))), max_pixels=9999) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/image")
            assert error.value.status == "too_many_pixels"

    asyncio.run(scenario())
    assert not list((tmp_path / "covers").iterdir())


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize("with_length", [True, False])
def test_byte_limit_declared_and_streamed(tmp_path, with_length):
    data = image_bytes()

    def handler(_):
        return httpx.Response(200, stream=Chunks([data[:30], data[30:]]), headers={"content-length": str(len(data))} if with_length else {})

    async def scenario():
        async with client(tmp_path, handler, max_bytes=40) as http:
            with pytest.raises(FetchError) as error:
                await http.download("https://example.test/image")
            assert error.value.status == "too_large"

    asyncio.run(scenario())
    assert not list((tmp_path / "covers").iterdir())


def test_json_cache_survives_client_restart_and_broken_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"images": [], "value": 7})

    async def scenario():
        async with client(tmp_path, handler) as http:
            result = await http.json("https://example.test/metadata", params={"a": "1", "b": "2"})
            assert result["value"] == 7
            await http.json("https://example.test/metadata", params={"b": "2", "a": "1"})
        async with client(tmp_path, handler) as http:
            assert await http.json("https://example.test/metadata", params={"a": "1", "b": "2"}) == result
            assert len(calls) == 1
            next((tmp_path / "cache").glob("*.json")).write_text("broken")
            await http.json("https://example.test/metadata", params={"a": "1", "b": "2"})
            assert len(calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"<html>Bad gateway</html>", b'{"incomplete":', b"[]"])
def test_invalid_json_is_not_cached(tmp_path, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    async def scenario():
        async with client(tmp_path, handler) as http:
            for _ in range(2):
                with pytest.raises(FetchError) as error:
                    await http.json("https://example.test/metadata")
                assert error.value.status == "invalid_json"

    asyncio.run(scenario())
    assert len(calls) == 2
    assert not list((tmp_path / "cache").iterdir())


def test_shared_musicbrainz_limiter_covers_retries_redirects_and_clients(tmp_path, monkeypatch):
    state = {"now": 0.0}
    calls = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        state["now"] += delay
        await real_sleep(0)

    monkeypatch.setattr(network, "time", SimpleNamespace(monotonic=lambda: state["now"], time=lambda: 1000))
    monkeypatch.setattr(network.asyncio, "sleep", sleep)
    monkeypatch.setattr(network.random, "uniform", lambda *_: 0)

    def handler(request):
        calls.append((state["now"], str(request.url)))
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with client(tmp_path, handler) as first, client(tmp_path, handler) as second:
            await first.json("https://musicbrainz.org/ws/2/release")
            await second.json("https://musicbrainz.org/ws/2/release-group")

    asyncio.run(scenario())
    assert len(calls) == 4
    assert all(right[0] - left[0] >= 1 for left, right in zip(calls, calls[1:]))


@pytest.fixture
def bandcamp_clock(monkeypatch):
    state = {"now": 0.0, "waits": []}
    real_sleep = asyncio.sleep

    async def sleep(delay):
        state["waits"].append(delay)
        state["now"] += delay
        await real_sleep(0)

    monkeypatch.setattr(network, "time", SimpleNamespace(monotonic=lambda: state["now"], time=lambda: 1000))
    monkeypatch.setattr(network.asyncio, "sleep", sleep)
    monkeypatch.setattr(network.random, "uniform", lambda *_: 0)
    return state


def test_bandcamp_pacing_shared_across_clients_domains_retries_and_redirects(tmp_path, bandcamp_clock):
    calls = []

    def handler(request):
        calls.append((bandcamp_clock["now"], str(request.url)))
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            return httpx.Response(302, headers={"location": "https://artist.bandcamp.com/album/final"})
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with client(tmp_path, handler) as first, client(tmp_path, handler) as second:
            await first.json("https://bandcamp.com/discover")
            await asyncio.gather(
                first.json("https://artist.bandcamp.com/album/next"),
                second.json("https://label.bandcamp.com/album/last"),
            )

    asyncio.run(scenario())
    assert len(calls) == 5
    assert [start for start, _ in calls] == pytest.approx([0, 1.1, 2.2, 3.3, 4.4])


@pytest.mark.parametrize("hostname", ["f4.bcbits.com", "notbandcamp.com", "bandcamp.com.example.test"])
def test_bandcamp_pacing_excludes_cdn_and_unrelated_hosts(tmp_path, bandcamp_clock, hostname):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with client(tmp_path, handler) as http:
            await http.json(f"https://{hostname}/one")
            await http.json(f"https://{hostname}/two")

    asyncio.run(scenario())
    assert len(calls) == 2 and bandcamp_clock["waits"] == []


@pytest.mark.parametrize("hostname", ["bandcamp.com", "artist.bandcamp.com"])
def test_bandcamp_429_no_retry_shared_cooldown_then_recovery(tmp_path, bandcamp_clock, hostname):
    calls = []
    responses = []

    def handler(request):
        calls.append(request)
        response = httpx.Response(429, headers={"retry-after": "7200"}) if len(calls) == 1 else httpx.Response(200, json={"ok": True})
        responses.append(response)
        return response

    async def scenario():
        async with client(tmp_path, handler, retries=3) as first, client(tmp_path, handler) as second:
            with pytest.raises(FetchError) as error:
                await first.post_json(f"https://{hostname}/discover", {"cursor": "*"}, fresh=True)
            assert error.value.status == "rate_limited"
            assert error.value.retry_after == 7200
            assert "429" in str(error.value)
            assert len(calls) == 1 and bandcamp_clock["waits"] == []
            assert responses[0].is_closed
            assert not list((tmp_path / "cache").iterdir())
            with pytest.raises(FetchError) as blocked:
                await second.text("https://other.bandcamp.com/album/later")
            assert blocked.value.status == "rate_limited" and blocked.value.retry_after == 7200
            assert len(calls) == 1
            # CDN requests may finish even after metadata receives 429.
            assert await second.json("https://f4.bcbits.com/unrelated") == {"ok": True}
            bandcamp_clock["now"] += 7201
            assert await first.post_json(f"https://{hostname}/discover", {"cursor": "*"}, fresh=True) == {"ok": True}

    asyncio.run(scenario())
    assert len(calls) == 3


@pytest.mark.parametrize("header", [None, "invalid", "NaN", "inf", "-1", "0"])
def test_bandcamp_429_without_valid_retry_after_has_finite_fallback(tmp_path, bandcamp_clock, header):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header} if header is not None else {})

    async def scenario():
        async with client(tmp_path, handler) as http:
            with pytest.raises(FetchError) as error:
                await http.text("https://artist.bandcamp.com/album/one")
            assert error.value.status == "rate_limited" and error.value.retry_after == 60

    asyncio.run(scenario())
    assert len(calls) == 1 and bandcamp_clock["waits"] == []


def test_bandcamp_429_interrupts_already_waiting_requests_even_after_short_cooldown(tmp_path, monkeypatch):
    state = {"now": 0.0}
    calls = []

    async def scenario():
        respond = asyncio.Event()
        sleeping = asyncio.Event()
        wake = asyncio.Event()

        async def sleep(delay):
            sleeping.set()
            await wake.wait()
            state["now"] += delay

        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                await respond.wait()
                return httpx.Response(429, headers={"retry-after": "0.1"})
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(network, "time", SimpleNamespace(monotonic=lambda: state["now"], time=lambda: 1000))
        monkeypatch.setattr(network.asyncio, "sleep", sleep)
        async with client(tmp_path, handler) as first, client(tmp_path, handler) as second:
            leading = asyncio.create_task(first.json("https://bandcamp.com/discover"))
            waiting = [asyncio.create_task(second.json(f"https://artist.bandcamp.com/{i}")) for i in range(2)]
            await sleeping.wait()
            respond.set()
            with pytest.raises(FetchError):
                await leading
            wake.set()
            errors = await asyncio.gather(*waiting, return_exceptions=True)
            assert all(isinstance(error, FetchError) and error.status == "rate_limited" for error in errors)
            assert len(calls) == 1
            # Only an explicit later request can begin after the cooldown ends.
            assert await first.json("https://bandcamp.com/discover") == {"ok": True}

    asyncio.run(scenario())
    assert len(calls) == 2


def test_download_concurrency_is_bounded(tmp_path):
    state = {"active": 0, "peak": 0}

    async def handler(_):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return httpx.Response(200, content=image_bytes())

    async def scenario():
        async with client(tmp_path, handler, concurrency=2) as http:
            downloads = await asyncio.gather(*(http.download(f"https://example.test/{i}") for i in range(6)))
            assert len({download.path for download in downloads}) == 1

    asyncio.run(scenario())
    assert state["peak"] == 2


def test_cancel_download_cleans_partial_file(tmp_path):
    async def scenario():
        started = asyncio.Event()
        blocked = asyncio.Event()

        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield image_bytes()[:30]
                started.set()
                await blocked.wait()

        async with client(tmp_path, lambda _: httpx.Response(200, stream=Slow())) as http:
            task = asyncio.create_task(http.download("https://example.test/slow"))
            await started.wait()
            assert list(tmp_path.rglob("*.part"))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert not list(tmp_path.rglob("*.part"))
    assert not list((tmp_path / "covers").iterdir())


def test_midstream_transport_error_retries_cleanly(tmp_path, monkeypatch):
    calls = []
    data = image_bytes()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(network.asyncio, "sleep", no_sleep)

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield data[:20]
            raise httpx.ReadError("connection dropped")

    def handler(_):
        calls.append(1)
        return httpx.Response(200, stream=Broken()) if len(calls) == 1 else httpx.Response(200, content=data)

    async def scenario():
        async with client(tmp_path, handler) as http:
            result = await http.download("https://example.test/image")
            assert result.path.read_bytes() == data

    asyncio.run(scenario())
    assert len(calls) == 2
    assert not list(tmp_path.rglob("*.part"))


def test_verify_existing_reuses_bytes_repairs_thumbnail_and_rejects_corruption(tmp_path):
    calls = []
    original = image_bytes()

    def handler(_):
        calls.append(1)
        return httpx.Response(200, content=original)

    async def scenario():
        async with client(tmp_path, handler) as http:
            result = await http.download("https://example.test/cover")
            metadata = {"sha256": result.sha256, "path": str(result.path.relative_to(tmp_path)), "downloaded_url": result.url}
            result.thumbnail_path.unlink()
            verified = http.verify_existing(metadata)
            assert verified == result
            assert verified.thumbnail_path.is_file()
            assert verified.path.read_bytes() == original
            assert len(calls) == 1
            verified.path.write_bytes(b"broken")
            assert http.verify_existing(metadata) is None
            assert verified.path.read_bytes() == b"broken"
            assert len(calls) == 1

    asyncio.run(scenario())


def test_verify_existing_respects_paths_limits_and_actual_format(tmp_path):
    async def scenario():
        async with client(tmp_path, lambda _: httpx.Response(200, content=image_bytes())) as http:
            result = await http.download("https://example.test/cover")
            metadata = {"sha256": result.sha256, "path": str(result.path)}
            http.max_pixels = 10
            assert http.verify_existing(metadata) is None
            http.max_pixels = 40_000_000
            renamed = result.path.with_suffix(".jpg")
            result.path.rename(renamed)
            assert http.verify_existing({**metadata, "path": str(renamed)}) is None
            assert renamed.is_file()
            assert http.verify_existing({**metadata, "path": "../outside.png"}) is None
            assert http.verify_existing({**metadata, "sha256": "bad"}) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["json", "download"])
def test_broken_http_content_encoding_retries_then_records_network_error(tmp_path, monkeypatch, kind):
    calls = []
    waits = []

    async def no_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(network.asyncio, "sleep", no_sleep)

    def handler(_):
        calls.append(1)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks([b"not gzip", b"invalid tail"]))

    async def scenario():
        async with client(tmp_path, handler, retries=1) as http:
            with pytest.raises(FetchError) as error:
                await getattr(http, kind)("https://example.test/corrupt-encoding")
            assert error.value.status == "network_error"
            assert isinstance(error.value.__cause__, httpx.DecodingError)

    asyncio.run(scenario())
    assert len(calls) == 2 and len(waits) == 1
    assert not list(tmp_path.rglob("*.part"))
    assert not list((tmp_path / "covers").iterdir())
    assert not list((tmp_path / "cache").iterdir())


@pytest.mark.parametrize("kind", ["json", "download"])
def test_broken_http_content_encoding_can_recover(tmp_path, monkeypatch, kind):
    calls = []
    original = image_bytes()

    async def no_sleep(_):
        pass

    monkeypatch.setattr(network.asyncio, "sleep", no_sleep)

    def handler(_):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Chunks([b"not gzip"]))
        if kind == "json":
            return httpx.Response(200, json={"images": []})
        return httpx.Response(200, content=original)

    async def scenario():
        async with client(tmp_path, handler, retries=1) as http:
            result = await getattr(http, kind)("https://example.test/recovered-encoding")
            if kind == "json":
                assert result == {"images": []}
            else:
                assert result.path.read_bytes() == original

    asyncio.run(scenario())
    assert len(calls) == 2
    assert not list(tmp_path.rglob("*.part"))


def test_post_cache_distinguishes_body_get_and_text_and_survives_restart(tmp_path):
    calls = []
    url = "https://example.test/discover"

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"method": request.method, "payload": json.loads(request.content) if request.content else None})

    async def scenario():
        async with client(tmp_path, handler) as http:
            first = await http.post_json(url, {"cursor": "one", "filter": {"a": 1, "b": 2}})
            assert first["payload"] == {"cursor": "one", "filter": {"a": 1, "b": 2}}
            assert await http.post_json(url, {"filter": {"b": 2, "a": 1}, "cursor": "one"}) == first
            second = await http.post_json(url, {"cursor": "two"})
            assert second["payload"] == {"cursor": "two"}
            assert await http.json(url) == {"method": "GET", "payload": None}
            assert json.loads(await http.text(url)) == {"method": "GET", "payload": None}
            assert len(calls) == 4
        async with client(tmp_path, handler) as restarted:
            assert await restarted.post_json(url, {"cursor": "one", "filter": {"a": 1, "b": 2}}) == first
            assert await restarted.post_json(url, {"cursor": "two"}) == second
            await restarted.json(url)
            await restarted.text(url)

    asyncio.run(scenario())
    assert len(calls) == 4
    assert all(request.headers["user-agent"] == "coverpacks/0.1.0 (https://example.test/coverpacks)" for request in calls)
    assert calls[0].headers["content-type"] == "application/json"


def test_fresh_post_replaces_same_payload_cache_and_survives_restart(tmp_path):
    calls = []
    url = "https://example.test/discover"
    payload = {"cursor": "*", "filter": {"a": 1, "b": 2}}

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"revision": len(calls)})

    async def scenario():
        async with client(tmp_path, handler) as http:
            assert await http.post_json(url, payload) == {"revision": 1}
            assert await http.post_json(url, payload) == {"revision": 1}
            assert await http.post_json(url, payload, fresh=True) == {"revision": 2}
            assert await http.post_json(url, payload) == {"revision": 2}
        async with client(tmp_path, handler) as restarted:
            assert await restarted.post_json(url, {"filter": {"b": 2, "a": 1}, "cursor": "*"}) == {"revision": 2}

    asyncio.run(scenario())
    assert len(calls) == 2
    assert all(request.method == "POST" and json.loads(request.content) == payload for request in calls)
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1


@pytest.mark.parametrize(("failure", "expected", "requests"), [(503, "network_error", 3), (200, "invalid_json", 2)])
def test_failed_fresh_post_raises_and_preserves_valid_cache(tmp_path, monkeypatch, failure, expected, requests):
    calls = []
    url = "https://example.test/discover"
    payload = {"cursor": "*"}

    async def sleep(_):
        pass

    monkeypatch.setattr(network.asyncio, "sleep", sleep)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"revision": 1})
        return httpx.Response(failure, content=b"<html>Temporary error</html>")

    async def scenario():
        async with client(tmp_path, handler, retries=1) as http:
            assert await http.post_json(url, payload) == {"revision": 1}
            cache = next((tmp_path / "cache").glob("*.json"))
            original = cache.read_bytes()
            with pytest.raises(FetchError) as error:
                await http.post_json(url, payload, fresh=True)
            assert error.value.status == expected
            assert cache.read_bytes() == original
            assert await http.post_json(url, payload) == {"revision": 1}

    asyncio.run(scenario())
    assert len(calls) == requests
    assert not list(tmp_path.rglob("*.part"))


def test_normal_post_waits_for_in_progress_fresh_response(tmp_path):
    calls = []
    url = "https://example.test/discover"
    payload = {"cursor": "*"}

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            calls.append(request)
            if len(calls) == 2:
                started.set()
                await release.wait()
            return httpx.Response(200, json={"revision": len(calls)})

        async with client(tmp_path, handler) as http:
            assert await http.post_json(url, payload) == {"revision": 1}
            refresh = asyncio.create_task(http.post_json(url, payload, fresh=True))
            await started.wait()
            normal = asyncio.create_task(http.post_json(url, payload))
            await asyncio.sleep(0)
            assert not normal.done()
            release.set()
            assert await refresh == {"revision": 2}
            assert await normal == {"revision": 2}

    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize("status", [429, 503])
def test_read_only_post_retry_preserves_body(tmp_path, monkeypatch, status):
    calls = []
    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(network.asyncio, "sleep", sleep)

    def handler(request):
        calls.append((request.method, json.loads(request.content)))
        return httpx.Response(status, headers={"retry-after": "2"}) if len(calls) == 1 else httpx.Response(200, json={"results": []})

    async def scenario():
        async with client(tmp_path, handler) as http:
            assert await http.post_json("https://example.test/discover", {"cursor": "abc"}) == {"results": []}

    asyncio.run(scenario())
    assert calls == [("POST", {"cursor": "abc"}), ("POST", {"cursor": "abc"})]
    assert waits == [2]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_post_redirect_method_and_body_semantics(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/start":
            return httpx.Response(status, headers={"location": "/final"})
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        async with client(tmp_path, handler) as http:
            assert await http.post_json("https://example.test/start", {"cursor": "abc"}) == {"ok": True}

    asyncio.run(scenario())
    assert len(calls) == 2
    if status in (307, 308):
        assert calls[1].method == "POST"
        assert json.loads(calls[1].content) == {"cursor": "abc"}
    else:
        assert calls[1].method == "GET" and not calls[1].content
        assert "content-type" not in calls[1].headers


def test_post_does_not_forward_body_to_another_origin(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://elsewhere.test/collect"})

    async def scenario():
        async with client(tmp_path, handler) as http:
            with pytest.raises(FetchError) as error:
                await http.post_json("https://example.test/discover", {"cursor": "abc"})
            assert error.value.status == "http_error"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert not list((tmp_path / "cache").iterdir())


def test_text_accepts_html_decodes_charset_and_expires_cache(tmp_path):
    calls = []
    html = "<!doctype html><html><title>Альбом</title></html>"

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=html.encode("windows-1251"), headers={"content-type": "text/html; charset=windows-1251"})

    async def scenario():
        async with client(tmp_path, handler) as http:
            assert await http.text("https://example.test/album") == html
            assert await http.text("https://example.test/album") == html
            assert len(calls) == 1
            path = next((tmp_path / "cache").glob("*.json"))
            cached = json.loads(path.read_text())
            cached["created_at"] = 0
            path.write_text(json.dumps(cached))
            assert await http.text("https://example.test/album") == html

    asyncio.run(scenario())
    assert len(calls) == 2


@pytest.mark.parametrize("with_length", [True, False])
def test_text_size_limit_declared_and_streamed(tmp_path, with_length):
    html = b"<html>" + b"x" * 100 + b"</html>"

    def handler(_):
        return httpx.Response(200, stream=Chunks([html[:20], html[20:]]), headers={"content-length": str(len(html))} if with_length else {})

    async def scenario():
        async with client(tmp_path, handler, max_bytes=30) as http:
            with pytest.raises(FetchError) as error:
                await http.text("https://example.test/album")
            assert error.value.status == "too_large"

    asyncio.run(scenario())
    assert not list((tmp_path / "cache").iterdir())


@pytest.mark.parametrize(("status", "expected"), [(404, "not_found"), (403, "blocked")])
def test_text_http_errors_are_not_cached_or_retried(tmp_path, status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="<html>Error</html>")

    async def scenario():
        async with client(tmp_path, handler) as http:
            with pytest.raises(FetchError) as error:
                await http.text("https://example.test/album")
            assert error.value.status == expected

    asyncio.run(scenario())
    assert len(calls) == 1
    assert not list((tmp_path / "cache").iterdir())

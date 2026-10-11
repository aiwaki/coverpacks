"""Private archive durability and hostile-boundary checks, without real HTTP."""
import asyncio
import base64
import copy
import io
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from coverpacks.cloud_store import (
    CloudArchive, CloudStateError, GitHubStore, bundle, digest, encoded,
    safe_name, unbundle,
)
from coverpacks.publisher import PublicationState, Publisher


class FakeGitHubStore:
    def __init__(self):
        self.private = True
        self.assets, self.pointer, self.sha = {}, None, None
        self.uploads, self.downloads, self.swaps, self.removed = [], [], [], []
        self.conflict = False

    def require_private(self):
        if not self.private:
            raise CloudStateError("Private repository required")

    def upload(self, content, kind):
        asset_id = max(self.assets, default=0) + 1
        self.assets[asset_id] = content
        self.uploads.append((kind, asset_id))
        return {"id": asset_id, "bytes": len(content), "sha256": digest(content)}

    def download(self, asset):
        self.downloads.append(asset["id"])
        content = self.assets[asset["id"]]
        if len(content) != asset["bytes"] or digest(content) != asset["sha256"]:
            raise CloudStateError("Archive checksum verification failed")
        return content

    def load_pointer(self):
        return copy.deepcopy(self.pointer), self.sha

    def compare_and_swap(self, pointer, previous_sha):
        self.swaps.append(previous_sha)
        if self.conflict or previous_sha != self.sha:
            raise CloudStateError("Concurrent cloud checkpoint")
        self.pointer = copy.deepcopy(pointer)
        self.sha = f"sha-{len(self.swaps)}"
        return self.sha

    def remove_checkpoint(self, asset):
        self.removed.append(asset["id"])
        self.assets.pop(asset["id"])

    def checkpoint_files(self):
        return unbundle(self.assets[self.pointer["checkpoint"]["id"]])

    def replace_checkpoint(self, files):
        self.pointer["checkpoint"] = self.upload(bundle(files), "test-checkpoint")


@pytest.fixture
def cloud(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE catalogue (entity TEXT PRIMARY KEY)")
    db.execute("INSERT INTO catalogue VALUES ('bandcamp:album:1:1')")
    db.commit()
    publication = PublicationState(root / "telegram")
    publication.put("account_id", 123)
    directory = root / "emoji" / "sleeve_vinyl"
    directory.mkdir(parents=True)
    manifest = encoded({"schema_version": 1, "packs": []})
    manifest_name = f"manifest-{digest(manifest)[:16]}.json"
    (directory / manifest_name).write_bytes(manifest)
    (directory / "latest.json").write_bytes(encoded({"manifest": manifest_name, "sha256": digest(manifest)}))
    for name, value in (("settings.json", {"source": "bandcamp"}), ("daily.json", {"interval_seconds": 86400}),
                        ("telegram/channel.json", {"username": "sleeve_test"})):
        (root / name).write_bytes(encoded(value))
    (root / "covers").mkdir()
    (root / "covers/a.png").write_bytes(b"cover A")
    (root / "covers/b.png").write_bytes(b"cover B")
    store = FakeGitHubStore()
    archive = CloudArchive(root, store)
    yield SimpleNamespace(root=root, db=db, publication=publication, store=store, archive=archive,
                          manifest_name=manifest_name, directory=directory)
    db.close()
    publication.close()


def rows(content, query):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(content)
        return db.execute(query).fetchall()
    finally:
        db.close()


def test_roundtrip_committed_wal_metadata_and_only_requested_media(cloud):
    pointer = cloud.archive.checkpoint()
    files = cloud.store.checkpoint_files()
    assert rows(files["state.sqlite3"], "SELECT entity FROM catalogue") == [("bandcamp:album:1:1",)]
    assert rows(files["telegram/publish.sqlite3"], "SELECT value FROM meta WHERE key='account_id'") == [("123",)]
    cloud.db.close()
    cloud.publication.close()
    shutil.rmtree(cloud.root)
    restored = CloudArchive(cloud.root, cloud.store)
    assert restored.restore() == pointer
    assert (cloud.root / "daily.json").is_file()
    assert not (cloud.root / "covers").exists()
    before = len(cloud.store.downloads)
    restored.hydrate([cloud.root / "covers/a.png"])
    assert (cloud.root / "covers/a.png").read_bytes() == b"cover A"
    assert not (cloud.root / "covers/b.png").exists()
    assert len(cloud.store.downloads) == before + 1
    restored.hydrate([cloud.root / "covers/a.png"])
    assert len(cloud.store.downloads) == before + 1
    assert (cloud.root / "telegram/publish.sqlite3").stat().st_mode & 0o777 == 0o600


def test_hydrate_groups_assets_and_replaces_corrupt_local_copy(cloud):
    cloud.archive.checkpoint()
    (cloud.root / "covers/a.png").write_bytes(b"wrong")
    (cloud.root / "covers/b.png").unlink()
    cloud.archive.hydrate([cloud.root / "covers/a.png", cloud.root / "covers/b.png"])
    assert len(cloud.store.downloads) == 1
    assert (cloud.root / "covers/a.png").read_bytes() == b"cover A"
    assert (cloud.root / "covers/b.png").read_bytes() == b"cover B"


def test_archive_does_not_include_credentials_session_cache_or_database_sidecars(cloud):
    for name in ("telegram/credentials.json", "telegram/account.session", "telegram/account.session-journal",
                 ".env", ".private/token", "cache/raw.json", "logs/debug.log", "auth.json"):
        path = cloud.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"DO-NOT-ARCHIVE")
    cloud.archive.checkpoint()
    archived = {name: content for content in cloud.store.assets.values() for name, content in unbundle(content).items()}
    assert not any(b"DO-NOT-ARCHIVE" in content for content in archived.values())
    assert not any(name.endswith(("-wal", "-shm", ".session")) or "credentials" in name for name in archived)
    assert set(archived) == {
        "covers/a.png", "covers/b.png", "media-index.json", "state.sqlite3", "telegram/publish.sqlite3",
        "settings.json", "daily.json", "telegram/channel.json", "emoji/sleeve_vinyl/latest.json",
        f"emoji/sleeve_vinyl/{cloud.manifest_name}",
    }


def test_checkpoint_reuses_immutable_media_and_retains_previous_checkpoint(cloud):
    first = cloud.archive.checkpoint()
    second = cloud.archive.checkpoint()
    assert second["previous"] == first["checkpoint"]
    assert cloud.store.removed == []
    third = cloud.archive.checkpoint()
    assert third["previous"] == second["checkpoint"]
    assert cloud.store.removed == [first["checkpoint"]["id"]]
    assert [kind for kind, _ in cloud.store.uploads].count("media") == 1
    assert cloud.store.swaps == [None, "sha-1", "sha-2"]


def test_changed_archived_media_stops_without_advancing_pointer(cloud):
    cloud.archive.checkpoint()
    old = copy.deepcopy(cloud.store.pointer)
    (cloud.root / "covers/a.png").write_bytes(b"changed after freeze")
    with pytest.raises(CloudStateError, match="Archived media changed"):
        cloud.archive.checkpoint()
    assert cloud.store.pointer == old


def test_stale_writer_cannot_overwrite_checkpoint(cloud):
    stale = CloudArchive(cloud.root, cloud.store)
    authoritative = cloud.archive.checkpoint()
    with pytest.raises(CloudStateError, match="Concurrent"):
        stale.checkpoint()
    assert cloud.store.pointer == authoritative
    assert stale.pointer is None and stale.pointer_sha is None


def test_publisher_two_checkpoints_commit_each_intent_before_mutation(cloud):
    observed = []

    async def checkpoint():
        cloud.archive.checkpoint()

    publisher = Publisher(cloud.publication, SimpleNamespace(), checkpoint=checkpoint)

    async def mutation():
        files = cloud.store.checkpoint_files()
        operation = rows(files["telegram/publish.sqlite3"], "SELECT value FROM meta WHERE key='last_operation'")[0][0]
        observed.append(json.loads(operation))

    async def run():
        await publisher._call("create_set:sleeve_test_001", mutation)
        await publisher._call("send_post:pack:sleeve_test_001:1", mutation)

    asyncio.run(run())
    assert observed == ["create_set:sleeve_test_001", "send_post:pack:sleeve_test_001:1"]
    assert cloud.store.swaps == [None, "sha-1"]


def test_publisher_cas_conflict_stops_next_mutation(cloud):
    writes = []

    async def checkpoint():
        cloud.archive.checkpoint()

    async def mutation():
        writes.append("external")

    publisher = Publisher(cloud.publication, SimpleNamespace(), checkpoint=checkpoint)
    asyncio.run(publisher._call("create_set:sleeve_test_001", mutation))
    cloud.store.conflict = True
    with pytest.raises(CloudStateError, match="Concurrent"):
        asyncio.run(publisher._call("send_post:pack:sleeve_test_001:1", mutation))
    assert writes == ["external"]


@pytest.mark.parametrize("missing", ["state.sqlite3", "telegram/publish.sqlite3", "media-index.json",
                                     "emoji/sleeve_vinyl/latest.json", "manifest"])
def test_restore_requires_complete_metadata_before_writing(cloud, missing):
    cloud.archive.checkpoint()
    files = cloud.store.checkpoint_files()
    files.pop(f"emoji/sleeve_vinyl/{cloud.manifest_name}" if missing == "manifest" else missing)
    cloud.store.replace_checkpoint(files)
    marker = cloud.root / "daily.json"
    marker.write_bytes(b"preserve existing local state")
    with pytest.raises(CloudStateError):
        CloudArchive(cloud.root, cloud.store).restore()
    assert marker.read_bytes() == b"preserve existing local state"


def test_missing_history_private_requirement_and_wrong_root_fail_closed(cloud):
    with pytest.raises(CloudStateError, match="history is missing"):
        cloud.archive.restore()
    cloud.archive.checkpoint()
    cloud.store.private = False
    with pytest.raises(CloudStateError, match="Private"):
        cloud.archive.restore()
    cloud.store.private = True
    with pytest.raises(CloudStateError, match="location/schema"):
        CloudArchive(cloud.root / "other", cloud.store).restore()


def test_checkpoint_requires_both_databases(cloud):
    cloud.publication.close()
    (cloud.root / "telegram/publish.sqlite3").unlink()
    with pytest.raises(CloudStateError, match="history is missing"):
        cloud.archive.checkpoint()
    assert cloud.store.pointer is None


def test_restore_verifies_manifest_checksum_and_rejects_extra_metadata(cloud):
    cloud.archive.checkpoint()
    original = cloud.store.checkpoint_files()
    changed = dict(original)
    changed[f"emoji/sleeve_vinyl/{cloud.manifest_name}"] = b"changed manifest"
    cloud.store.replace_checkpoint(changed)
    with pytest.raises(CloudStateError, match="verification"):
        cloud.archive.restore()
    cloud.store.replace_checkpoint(dict(original, **{"telegram/credentials.json": b"not allowed"}))
    with pytest.raises(CloudStateError, match="Unexpected file"):
        cloud.archive.restore()


def test_checkpoint_and_restore_keep_active_manifest_alongside_latest(cloud):
    active = encoded({"schema_version": 1, "packs": ["active"]})
    active_path = cloud.directory / f"manifest-{digest(active)[:16]}.json"
    active_path.write_bytes(active)
    cloud.publication.put("daily_active_pack", {"manifest_path": str(active_path), "manifest_hash": digest(active)})
    cloud.archive.checkpoint()
    assert cloud.archive.relative(active_path) in cloud.store.checkpoint_files()
    cloud.publication.close()
    cloud.archive.restore()
    files = cloud.store.checkpoint_files()
    files.pop(cloud.archive.relative(active_path))
    cloud.store.replace_checkpoint(files)
    with pytest.raises(CloudStateError, match="Active manifest"):
        cloud.archive.restore()


@pytest.mark.parametrize("failure", ["missing_index", "missing_file", "wrong_hash", "archive_checksum"])
def test_required_media_failures_do_not_create_local_asset(cloud, failure):
    cloud.archive.checkpoint()
    path = cloud.root / "covers/a.png"
    path.unlink()
    entry = cloud.archive.media["covers/a.png"]
    if failure == "missing_index":
        cloud.archive.media.pop("covers/a.png")
    elif failure == "missing_file":
        entry["asset"] = cloud.store.upload(bundle({"covers/other.png": b"wrong file"}), "test")
    elif failure == "wrong_hash":
        entry["sha256"] = "0" * 64
    else:
        cloud.store.assets[entry["asset"]["id"]] = b"corrupt archive bytes"
    with pytest.raises(CloudStateError):
        cloud.archive.hydrate([path])
    assert not path.exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../../outside", "a//b", "a/./b",
                                  ".hidden/file", "telegram/account.session", "bad\\path", "nul\x00name", "."])
def test_archive_rejects_unsafe_or_alias_paths(name):
    with pytest.raises(CloudStateError):
        safe_name(name)


def test_unbundle_rejects_traversal_duplicate_entries_and_expansion_limit():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside", b"escape")
    with pytest.raises(CloudStateError, match="Unsafe"):
        unbundle(buffer.getvalue())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("same", b"first")
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("same", b"second")
    with pytest.raises(CloudStateError, match="Invalid"):
        unbundle(buffer.getvalue())
    with pytest.raises(CloudStateError, match="oversized"):
        unbundle(bundle({"large": b"x" * 33}), maximum=32)


@pytest.mark.parametrize("parent", [False, True])
def test_restore_rejects_destination_symlink_including_parent_before_writes(cloud, parent):
    cloud.archive.checkpoint()
    cloud.publication.close()
    outside = cloud.root.parent / "outside"
    outside.mkdir()
    if parent:
        shutil.rmtree(cloud.root / "telegram")
        (cloud.root / "telegram").symlink_to(outside, target_is_directory=True)
    else:
        (cloud.root / "telegram/publish.sqlite3").unlink()
        (cloud.root / "telegram/publish.sqlite3").symlink_to(outside / "target.sqlite3")
    (cloud.root / "daily.json").write_bytes(b"unchanged")
    with pytest.raises(CloudStateError, match="Symlinks"):
        cloud.archive.restore()
    assert list(outside.iterdir()) == []
    assert (cloud.root / "daily.json").read_bytes() == b"unchanged"


def test_hydrate_and_checkpoint_reject_symlink_parent(cloud):
    cloud.archive.checkpoint()
    outside = cloud.root.parent / "external"
    outside.mkdir()
    (outside / "a.png").write_bytes(b"private external content")
    shutil.rmtree(cloud.root / "covers")
    (cloud.root / "covers").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CloudStateError, match="Symlinks"):
        cloud.archive.hydrate([cloud.root / "covers/a.png"])
    with pytest.raises(CloudStateError, match="Symlinks"):
        cloud.archive.checkpoint()
    assert (outside / "a.png").read_bytes() == b"private external content"


def descriptor(content=b"release archive"):
    return {"id": 7, "bytes": len(content), "sha256": digest(content)}


def test_signed_download_does_not_inherit_auth_cookies_or_basic_auth():
    observed = []
    content = b"release archive"

    def handler(request):
        observed.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer private-test-token"
            return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/asset?signature=private"})
        assert request.url.host == "release-assets.githubusercontent.com"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "should-not-leak"},
                      cookies={"session": "should-not-leak"}, auth=("private-user", "private-password"),
                      follow_redirects=True) as client:
        assert GitHubStore("owner/private", "private-test-token", client=client).download(descriptor(content)) == content
    assert len(observed) == 2


@pytest.mark.parametrize("url", ["http://release-assets.githubusercontent.com/asset", "https://evil.example/asset",
                                 "https://user:password@release-assets.githubusercontent.com/asset", ""])
def test_download_rejects_unexpected_redirect_without_following(url):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": url})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CloudStateError, match="Unexpected"):
            GitHubStore("owner/private", "private-test-token", client=client).download(descriptor())
    assert len(requests) == 1


def test_signed_download_does_not_follow_another_redirect():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/first"
                                          if len(requests) == 1 else "https://evil.example/next"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(CloudStateError, match="download failed"):
            GitHubStore("owner/private", "private-test-token", client=client).download(descriptor())
    assert len(requests) == 2


class CountingStream(httpx.SyncByteStream):
    def __init__(self):
        self.consumed, self.closed = 0, False

    def __iter__(self):
        for _ in range(100):
            self.consumed += 1
            yield b"x" * (64 * 1024)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("advertised", [False, True])
def test_download_is_bounded_before_buffering_oversized_body(advertised):
    stream = CountingStream()

    def handler(request):
        return httpx.Response(200, stream=stream, headers={"Content-Length": "999999"} if advertised else {})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CloudStateError, match="declared size"):
            GitHubStore("owner/private", "token", client=client).download(descriptor(b"x" * 10))
    assert stream.consumed == (0 if advertised else 1)
    assert stream.closed


@pytest.mark.parametrize("asset", [{"id": True, "bytes": 5, "sha256": "a" * 64},
                                  {"id": 1, "bytes": True, "sha256": "a" * 64},
                                  {"id": 1, "bytes": 1024**3, "sha256": "a" * 64},
                                  {"id": 1, "bytes": 5, "sha256": "invalid"}])
def test_invalid_download_descriptor_is_rejected_before_http(asset):
    def handler(request):
        pytest.fail("descriptor validation must precede HTTP")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CloudStateError, match="descriptor"):
            GitHubStore("owner/private", "token", client=client).download(asset)


def test_download_verifies_actual_checksum_and_redacts_connection_errors():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"same length!!!!"))) as client:
        with pytest.raises(CloudStateError, match="checksum"):
            GitHubStore("owner/private", "token", client=client).download(descriptor())

    def failed(request):
        raise httpx.ReadError("secret-token-in-network-diagnostic")

    with httpx.Client(transport=httpx.MockTransport(failed)) as client:
        with pytest.raises(CloudStateError) as caught:
            GitHubStore("owner/private", "token", client=client).download(descriptor())
    assert "secret-token" not in str(caught.value)


def test_cas_lost_acknowledgement_reconciles_identical_pointer_without_rewriting():
    pointer, observed = {"generation": "new"}, []

    def handler(request):
        observed.append(request)
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert payload["sha"] == "old-sha"
            assert json.loads(base64.b64decode(payload["content"])) == pointer
            raise httpx.ReadTimeout("acknowledgement lost after server commit")
        return httpx.Response(200, json={"content": base64.b64encode(encoded(pointer)).decode(), "sha": "new-sha"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert GitHubStore("owner/private", "token", client=client).compare_and_swap(pointer, "old-sha") == "new-sha"
    assert [request.method for request in observed] == ["PUT", "GET"]


def test_cas_conflict_does_not_retry_or_expose_response_body():
    observed = []

    def handler(request):
        observed.append(request)
        if request.method == "PUT":
            return httpx.Response(409, text="sensitive server details")
        return httpx.Response(200, json={"content": base64.b64encode(encoded({"generation": "winner"})).decode(), "sha": "winner-sha"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CloudStateError, match="not confirmed") as caught:
            GitHubStore("owner/private", "token", client=client).compare_and_swap({"generation": "loser"}, "old-sha")
    assert "sensitive" not in str(caught.value)
    assert [request.method for request in observed] == ["PUT", "GET"]

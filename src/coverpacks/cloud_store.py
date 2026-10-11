"""Private GitHub release archives with a compare-and-swap state pointer.

Media is immutable. A new database checkpoint becomes authoritative only after
the pointer update succeeds; callers must checkpoint before publishing anything.
Neither Telegram credentials nor sessions are part of this archive format.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import uuid
import zipfile

import httpx


class CloudStateError(RuntimeError):
    pass


def digest(content):
    return hashlib.sha256(content).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def safe_name(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise CloudStateError("Unsafe archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or path.as_posix() != name or name == ".":
        raise CloudStateError("Unsafe archive path")
    if any(part.startswith(".") for part in path.parts) or ".session" in name:
        raise CloudStateError("Private files cannot be archived")
    return name


def bundle(files):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(safe_name(name))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return target.getvalue()


def unbundle(content, *, maximum=512 * 1024**2):
    result, size = {}, 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            name = safe_name(info.filename)
            size += info.file_size
            if name in result or info.is_dir() or size > maximum:
                raise CloudStateError("Invalid or oversized archive")
            result[name] = archive.read(info)
    return result


class GitHubStore:
    """Authenticated API client; error messages deliberately omit response bodies."""
    def __init__(self, repository, token, *, client=None, branch="coverpacks-state"):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not token:
            raise CloudStateError("A private GitHub repository and token are required")
        self.repository, self.branch = repository, branch
        self.client = client or httpx.Client(timeout=180)
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.base = f"https://api.github.com/repos/{repository}"
        self.releases = {}

    def request(self, method, path, **kwargs):
        headers = dict(self.headers, **kwargs.pop("headers", {}))
        try:
            response = self.client.request(method, self.base + path, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise CloudStateError(f"GitHub {method}: connection failed") from None
        if response.status_code not in {200, 201, 204, 302, 404}:
            raise CloudStateError(f"GitHub {method}: HTTP {response.status_code}; stopped without publishing")
        return response

    def require_private(self):
        response = self.request("GET", "")
        if response.status_code != 200 or response.json().get("private") is not True:
            raise CloudStateError("Cloud publishing requires a private repository")

    def load_pointer(self):
        response = self.request("GET", "/contents/checkpoint.json", params={"ref": self.branch})
        if response.status_code == 404:
            return None, None
        value = response.json()
        return json.loads(base64.b64decode(value["content"])), value["sha"]

    def compare_and_swap(self, pointer, previous_sha):
        payload = {"message": "Checkpoint SLEEVE publication", "branch": self.branch,
                   "content": base64.b64encode(encoded(pointer)).decode()}
        if previous_sha:
            payload["sha"] = previous_sha
        try:
            response = self.request("PUT", "/contents/checkpoint.json", json=payload)
            if response.status_code not in {200, 201}:
                raise CloudStateError("State branch is unavailable")
            return response.json()["content"]["sha"]
        except CloudStateError:
            # A lost acknowledgement must not turn into an unconditional write.
            observed, sha = self.load_pointer()
            if observed == pointer:
                return sha
            raise CloudStateError("Cloud checkpoint was not confirmed; Telegram writes stopped") from None

    def upload(self, content, kind):
        if len(content) >= 1024**3:
            raise CloudStateError("Archive exceeds the application's 1 GiB safety limit")
        tag = "coverpacks-" + datetime.now(timezone.utc).strftime("%Y-%m")
        release = self.releases.get(tag)
        if release is None:
            response = self.request("GET", f"/releases/tags/{tag}")
            if response.status_code == 404:
                response = self.request("POST", "/releases", json={"tag_name": tag,
                    "name": f"SLEEVE private archives · {tag[-7:]}", "draft": False,
                    "prerelease": True, "make_latest": "false"})
            release = response.json()["id"]
            self.releases[tag] = release
        name = f"{kind}-{uuid.uuid4().hex}.zip"
        url = f"https://uploads.github.com/repos/{self.repository}/releases/{release}/assets"
        try:
            response = self.client.post(url, headers=dict(self.headers, **{"Content-Type": "application/zip"}),
                                        params={"name": name}, content=content)
        except httpx.HTTPError:
            raise CloudStateError("Archive upload failed; no checkpoint committed") from None
        if response.status_code != 201:
            raise CloudStateError(f"Archive upload failed: HTTP {response.status_code}")
        value = response.json()
        if value.get("size") != len(content):
            raise CloudStateError("Archive size verification failed")
        if value.get("digest") and value["digest"] != "sha256:" + digest(content):
            raise CloudStateError("Archive checksum verification failed")
        return {"id": value["id"], "sha256": digest(content), "bytes": len(content)}

    def download(self, asset):
        if (not isinstance(asset, dict) or type(asset.get("id")) is not int or asset["id"] <= 0
                or type(asset.get("bytes")) is not int or not 0 < asset["bytes"] < 1024**3
                or not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])):
            raise CloudStateError("Invalid archive descriptor")
        response = None
        headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity", "User-Agent": "coverpacks"}
        try:
            request = httpx.Request("GET", f"{self.base}/releases/assets/{asset['id']}",
                                    headers=dict(self.headers, **headers))
            response = self.client.send(request, auth=None, follow_redirects=False, stream=True)
            if response.status_code == 302:
                # Construct a fresh request instead of inheriting client defaults,
                # including cookies or Authorization from an injected HTTP client.
                url = httpx.URL(response.headers.get("Location", ""))
                if (url.scheme != "https" or url.host != "release-assets.githubusercontent.com"
                        or url.username or url.password):
                    raise CloudStateError("Unexpected release download host")
                response.close()
                response = self.client.send(httpx.Request("GET", url, headers=headers),
                                            auth=None, follow_redirects=False, stream=True)
            if response.status_code != 200:
                raise CloudStateError("Archive download failed")
            advertised = response.headers.get("Content-Length")
            if advertised is not None and (not advertised.isdecimal() or int(advertised) > asset["bytes"]):
                raise CloudStateError("Archive exceeds its declared size")
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(content) + len(chunk) > asset["bytes"]:
                    raise CloudStateError("Archive exceeds its declared size")
                content.extend(chunk)
        except httpx.HTTPError:
            raise CloudStateError("Archive download failed: connection error") from None
        finally:
            if response is not None:
                response.close()
        if len(content) != asset["bytes"] or digest(content) != asset["sha256"]:
            raise CloudStateError("Archive checksum verification failed")
        return bytes(content)

    def remove_checkpoint(self, asset):
        # Only obsolete checkpoint descriptors are passed here, never media.
        self.request("DELETE", f"/releases/assets/{asset['id']}")


def sqlite_snapshot(path):
    with sqlite3.connect(path) as source, sqlite3.connect(":memory:") as destination:
        source.backup(destination)
        content = destination.serialize()
        # backup includes committed WAL pages, but serialize preserves its WAL
        # header flags. SQLite documents this conversion for standalone images:
        # https://www.sqlite.org/c3ref/deserialize.html
        return content[:18] + b"\x01\x01" + content[20:]


class CloudArchive:
    def __init__(self, root, store, *, prefix="sleeve_vinyl"):
        self.root = Path(root).resolve()
        self.store, self.prefix = store, prefix
        self.pointer, self.pointer_sha = None, None
        self.media = {}

    def relative(self, path):
        try:
            name = safe_name(str(Path(path).absolute().relative_to(self.root)))
        except ValueError:
            raise CloudStateError("Frozen asset is outside the data directory") from None
        self.local_path(name)
        return name

    def local_path(self, name):
        path = self.root
        if path.is_symlink():
            raise CloudStateError("Symlinks are not supported in the data directory")
        for part in PurePosixPath(safe_name(name)).parts:
            path = path / part
            if path.is_symlink():
                raise CloudStateError("Symlinks are not supported in the data directory")
        return path

    def restore(self):
        self.store.require_private()
        self.pointer, self.pointer_sha = self.store.load_pointer()
        if self.pointer is None:
            raise CloudStateError("Cloud history is missing; refusing to start from an empty catalogue")
        if self.pointer.get("schema") != 1 or self.pointer.get("root") != str(self.root):
            raise CloudStateError("Cloud dataset location/schema differs from its frozen plan")
        files = unbundle(self.store.download(self.pointer["checkpoint"]))
        latest_name = f"emoji/{self.prefix}/latest.json"
        required = {"state.sqlite3", "telegram/publish.sqlite3", "media-index.json", latest_name}
        if not required <= files.keys():
            raise CloudStateError("Required publication history is missing from checkpoint")
        try:
            media = json.loads(files.pop("media-index.json"))
            latest = json.loads(files[latest_name])
            manifest_name = f"emoji/{self.prefix}/{latest['manifest']}"
            if (not isinstance(media, dict) or not self.metadata_name(manifest_name)
                    or manifest_name not in files or digest(files[manifest_name]) != latest["sha256"]):
                raise CloudStateError("Manifest or media index failed checkpoint verification")
            with sqlite3.connect(":memory:") as db:
                db.deserialize(files["telegram/publish.sqlite3"])
                row = db.execute("SELECT value FROM meta WHERE key='daily_active_pack'").fetchone()
                active = json.loads(row[0]) if row else None
            if active:
                active_name = self.relative(active["manifest_path"])
                if (not self.metadata_name(active_name) or active_name not in files
                        or digest(files[active_name]) != active["manifest_hash"]):
                    raise CloudStateError("Active manifest is missing or changed in checkpoint")
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            raise CloudStateError("Invalid checkpoint metadata") from None
        if any(not self.metadata_name(name) for name in files):
            raise CloudStateError("Unexpected file in metadata archive")
        # Validate every destination before writing even the first metadata file.
        destinations = {name: self.local_path(name) for name in files}
        self.media = media
        for name, content in files.items():
            path = destinations[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
        return self.pointer

    def metadata_name(self, name):
        return (name in {"state.sqlite3", "settings.json", "daily.json", "telegram/publish.sqlite3", "telegram/channel.json"}
                or bool(re.fullmatch(r"emoji/" + re.escape(self.prefix) + r"/(latest|manifest-[a-f0-9]{16})\.json", name)))

    def hydrate(self, paths):
        groups = {}
        for path in paths:
            name = self.relative(path)
            entry = self.media.get(name)
            if not entry:
                raise CloudStateError(f"Required asset has no durable archive: {name}")
            if Path(path).is_file() and digest(Path(path).read_bytes()) == entry["sha256"]:
                continue
            groups.setdefault(entry["asset"]["id"], (entry["asset"], []))[1].append(name)
        for asset, names in groups.values():
            files = unbundle(self.store.download(asset), maximum=1024**3)
            for name in names:
                content = files.get(name)
                if content is None or digest(content) != self.media[name]["sha256"]:
                    raise CloudStateError("Required media failed archive verification")
                path = self.local_path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

    def checkpoint(self):
        new_files = {}
        batch_bytes = 0
        def flush_media():
            if new_files:
                asset = self.store.upload(bundle(new_files), "media")
                for name, content in new_files.items():
                    self.media[name] = {"asset": asset, "sha256": digest(content)}
                new_files.clear()
        patterns = ("covers/*", "thumbnails/*", f"emoji/{self.prefix}/assets/*", f"emoji/{self.prefix}/preview-*.png",
                    "telegram/previews/*/*.jpg", "telegram/branding/*.png")
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    continue
                name, content = self.relative(path), path.read_bytes()
                previous = self.media.get(name)
                if previous:
                    if previous["sha256"] != digest(content):
                        raise CloudStateError(f"Archived media changed: {name}")
                else:
                    if new_files and batch_bytes + len(content) > 128 * 1024**2:
                        flush_media()
                        batch_bytes = 0
                    new_files[name] = content
                    batch_bytes += len(content)
        flush_media()
        files = {"media-index.json": encoded(self.media)}
        for name in ("state.sqlite3", "telegram/publish.sqlite3"):
            path = self.local_path(name)
            if not path.is_file():
                raise CloudStateError("Required publication history is missing")
            files[name] = sqlite_snapshot(path)
        for name in ("settings.json", "daily.json", "telegram/channel.json"):
            path = self.local_path(name)
            if path.is_file():
                files[name] = path.read_bytes()
        directory = self.local_path(f"emoji/{self.prefix}")
        pointer = json.loads(self.local_path(f"emoji/{self.prefix}/latest.json").read_bytes())
        selected = {pointer["manifest"]}
        with sqlite3.connect(self.root / "telegram/publish.sqlite3") as db:
            row = db.execute("SELECT value FROM meta WHERE key='daily_active_pack'").fetchone()
            active = json.loads(row[0]) if row else None
            if active:
                selected.add(Path(active["manifest_path"]).name)
        for name in selected | {"latest.json"}:
            path = directory / name
            relative = self.relative(path)
            if not self.metadata_name(relative):
                raise CloudStateError("Invalid manifest pointer")
            files[relative] = path.read_bytes()
        asset = self.store.upload(bundle(files), "checkpoint")
        pointer = {"schema": 1, "root": str(self.root), "generation": uuid.uuid4().hex,
                   "checkpoint": asset, "previous": self.pointer.get("checkpoint") if self.pointer else None}
        old = self.pointer
        sha = self.store.compare_and_swap(pointer, self.pointer_sha)
        self.pointer, self.pointer_sha = pointer, sha
        if old and old.get("previous"):
            try:
                self.store.remove_checkpoint(old["previous"])
            except CloudStateError:
                pass  # Cleanup cannot invalidate a committed checkpoint.
        return pointer

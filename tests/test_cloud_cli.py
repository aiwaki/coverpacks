"""Cloud modes and failure handling with no real store or Telegram connection."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from coverpacks import cloud
from coverpacks.cloud_store import CloudStateError
from coverpacks.publisher import PublicationState
from coverpacks.state import State


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "data"
    events = []
    case = SimpleNamespace(root=root, events=events, next_due_at=None, account_id=123,
                           channel_valid=True, set_exists=True, checkpoints=[], schedule_ahead_seconds=0)
    monkeypatch.setenv("GH_TOKEN", "test-github-token")
    monkeypatch.setenv("COVERPACKS_TELEGRAM", "deliberately invalid test secret")

    class Archive:
        def __init__(self, data_dir, store):
            self.root = Path(data_dir).resolve()
            self.media = {"telegram/branding/avatar.png": {}}

        def restore(self):
            events.append("restore")
            state = State(root)
            state.close()
            config = {"title": "SLEEVE", "about": "", "username": "sleeve_test",
                      "avatar": str(root / "telegram/branding/avatar.png")}
            ledger = PublicationState(root / "telegram")
            ledger.put("account_id", 123)
            ledger.put("channel", {"id": 10, "access_hash": 20})
            ledger.put("daily_next_due_at", case.next_due_at)
            with ledger.db:
                ledger.db.execute("INSERT INTO packs(number,short_name,pack_hash,payload,status) VALUES(1,?,?,?,'published')",
                                  ("sleeve_test_001", "0" * 64, "{}"))
            ledger.close()
            (root / "daily.json").write_text(json.dumps({"prefix": "sleeve_vinyl", "task_id": 1,
                "schedule_ahead_seconds": case.schedule_ahead_seconds}))
            (root / "telegram/channel.json").write_text(json.dumps(config))
            directory = root / "emoji/sleeve_vinyl"
            directory.mkdir(parents=True)
            content = json.dumps({"packs": [{"short_name": "sleeve_test_001"}]}).encode()
            digest = hashlib.sha256(content).hexdigest()
            manifest = f"manifest-{digest[:16]}.json"
            (directory / manifest).write_bytes(content)
            (directory / "latest.json").write_text(json.dumps({"manifest": manifest, "sha256": digest}))

        def hydrate(self, paths):
            events.append(("hydrate", set(paths)))

        def checkpoint(self):
            events.append("checkpoint")
            db = sqlite3.connect(root / "telegram/publish.sqlite3")
            try:
                case.checkpoints.append(dict(db.execute("SELECT key,value FROM meta")))
            finally:
                db.close()

    class Transport:
        async def me(self):
            events.append("me")
            return {"id": case.account_id}

        async def verify_channel(self, channel, config):
            events.append("verify_channel")
            return case.channel_valid

        async def get_set(self, name):
            events.append(("get_set", name))
            return {"short_name": name} if case.set_exists else None

    @asynccontextmanager
    async def transport_factory():
        events.append("connect")
        try:
            yield Transport()
        finally:
            events.append("disconnect")

    async def unexpected_daily(*args, **kwargs):
        pytest.fail("This mode must not enter daily publication")

    async def probe_source(settings, data_dir):
        events.append("probe_source")
        assert data_dir == root and settings["prefix"] == "sleeve_vinyl"
        return {"candidates": 1, "cover_width": 1200, "cover_height": 1200, "cover_bytes": 10000}

    monkeypatch.setattr(cloud, "GitHubStore", lambda repository, token: object())
    monkeypatch.setattr(cloud, "CloudArchive", Archive)
    monkeypatch.setattr(cloud, "telegram_transport", transport_factory)
    monkeypatch.setattr(cloud, "run_daily", unexpected_daily)
    monkeypatch.setattr(cloud, "probe_source", probe_source)
    return case


def execute(case, mode):
    return asyncio.run(cloud.execute(SimpleNamespace(repository="owner/private", data_dir=case.root, mode=mode)))


def test_preflight_transfers_archive_asset_without_opening_telegram_or_checkpoint(case, capsys):
    execute(case, "preflight")
    assert case.events == ["restore", ("hydrate", {case.root / "telegram/branding/avatar.png"}), "probe_source"]
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "preflight_ok" and result["bandcamp"]["candidates"] == 1


def test_not_due_run_does_not_hydrate_connect_or_checkpoint(case, capsys):
    case.next_due_at = 2**40
    execute(case, "run")
    assert case.events == ["restore"]
    assert json.loads(capsys.readouterr().out) == {"status": "not_due", "next_due_at": 2**40}


def test_cloud_enters_preparation_window_before_delivery_time(case, monkeypatch, capsys):
    import time
    case.next_due_at = time.time() + 3600
    case.schedule_ahead_seconds = 7200
    monkeypatch.setattr(cloud, "needed_media", lambda *args: set())
    calls = []

    async def daily(state, ledger, config, settings, factory, checkpoint):
        calls.append(settings["schedule_ahead_seconds"])
        return {"status": "scheduled"}

    monkeypatch.setattr(cloud, "run_daily", daily)
    execute(case, "run")
    assert calls == [7200] and json.loads(capsys.readouterr().out)["status"] == "scheduled"


def test_verify_only_checks_account_channel_and_existing_set(case, capsys):
    execute(case, "verify")
    assert case.events == ["restore", ("hydrate", {case.root / "telegram/branding/avatar.png"}),
                           "connect", "me", "verify_channel", ("get_set", "sleeve_test_001"), "disconnect"]
    assert json.loads(capsys.readouterr().out) == {"status": "telegram_verified", "packs": 1, "writes": 0}


@pytest.mark.parametrize("attribute,value", [("account_id", 999), ("channel_valid", False), ("set_exists", False)])
def test_verify_mismatch_fails_without_checkpoint_and_disconnects(case, attribute, value):
    setattr(case, attribute, value)
    with pytest.raises(CloudStateError):
        execute(case, "verify")
    assert "checkpoint" not in case.events and case.events[-1] == "disconnect"


@pytest.mark.parametrize("name", ["state.sqlite3", "telegram/publish.sqlite3-wal", "nested/old.sqlite3-shm"])
def test_fresh_root_guard_rejects_database_or_sidecars_before_restore(case, name):
    path = case.root / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"preserve local history")
    with pytest.raises(CloudStateError, match="fresh data directory"):
        execute(case, "preflight")
    assert case.events == [] and path.read_bytes() == b"preserve local history"


def test_daily_failure_preserves_latest_local_progress_and_does_not_repeat_work(case, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "needed_media", lambda state, ledger, config: {case.root / "needed.png"})

    async def fail(state, ledger, config, settings, transport_factory, checkpoint):
        calls.append("daily")
        await checkpoint()
        ledger.put("test_progress", {"completed": 3})
        raise OSError("simulated collection interruption")

    monkeypatch.setattr(cloud, "run_daily", fail)
    with pytest.raises(OSError):
        execute(case, "run")
    assert calls == ["daily"]
    assert case.events == ["restore", ("hydrate", {case.root / "needed.png"}), "checkpoint", "checkpoint"]
    assert "test_progress" not in case.checkpoints[0]
    assert json.loads(case.checkpoints[1]["test_progress"]) == {"completed": 3}


def test_cli_error_output_never_prints_rpc_or_http_exception_details(monkeypatch, capsys, tmp_path):
    async def failed(args):
        raise RuntimeError("private Telegram session and signed GitHub URL")

    monkeypatch.setattr(cloud, "execute", failed)
    assert cloud.main(["--repository", "owner/private", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Cloud run stopped: RuntimeError. Saved history will be reconciled on retry.\n"


def test_transport_disconnect_checkpoints_and_reconnects_with_saved_progress(case, monkeypatch, capsys):
    monkeypatch.setattr(cloud, "needed_media", lambda *args: set())
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    async def daily(state, ledger, config, settings, factory, checkpoint):
        async with factory():
            calls.append(ledger.get("accepted_post"))
            if len(calls) == 1:
                ledger.put("accepted_post", 42)
                raise ConnectionResetError("private transport details")
            assert json.loads(case.checkpoints[-1]["accepted_post"]) == 42
            return {"status": "published", "reconciled_message_id": 42}

    monkeypatch.setattr(cloud.asyncio, "sleep", sleep)
    monkeypatch.setattr(cloud, "run_daily", daily)
    execute(case, "run")
    assert calls == [None, 42] and sleeps == [5]
    assert case.events.count("connect") == case.events.count("disconnect") == 2
    assert case.events.index("checkpoint") < case.events.index("connect", case.events.index("disconnect"))
    output = capsys.readouterr().out
    assert "private transport details" not in output
    assert json.loads(output.splitlines()[-1])["reconciled_message_id"] == 42


@pytest.mark.parametrize("error", [ConnectionResetError, TimeoutError])
def test_transport_retry_is_bounded_and_saves_each_failure(case, monkeypatch, error):
    monkeypatch.setattr(cloud, "needed_media", lambda *args: set())
    calls, sleeps = [], []

    async def daily(*args):
        calls.append(1)
        raise error("unavailable")

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(cloud, "run_daily", daily)
    monkeypatch.setattr(cloud.asyncio, "sleep", sleep)
    with pytest.raises(error):
        execute(case, "run")
    assert len(calls) == 3 and sleeps == [5, 15]
    assert len(case.checkpoints) >= 3


def test_retry_checkpoint_failure_stops_before_reconnect(monkeypatch):
    calls = []

    async def daily(*args):
        calls.append("daily")
        raise ConnectionResetError()

    async def checkpoint():
        calls.append("checkpoint")
        raise CloudStateError("CAS not confirmed")

    monkeypatch.setattr(cloud, "run_daily", daily)
    with pytest.raises(CloudStateError):
        asyncio.run(cloud.resumable_daily(None, None, None, None, checkpoint))
    assert calls == ["daily", "checkpoint"]


@pytest.mark.parametrize("result", [{"status": "deferred"}, {"status": "not_due"}])
def test_daily_terminal_result_does_not_retry_or_wait(monkeypatch, result):
    calls = []

    async def daily(*args):
        calls.append("daily")
        return result

    async def checkpoint():
        pytest.fail("No checkpoint needed for this result")

    monkeypatch.setattr(cloud, "run_daily", daily)
    assert asyncio.run(cloud.resumable_daily(None, None, None, None, checkpoint)) == result
    assert calls == ["daily"]

"""Cloud entrypoint. Secrets are held in memory, outside all snapshots."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession

from .bandcamp import BandcampAPI
from .cloud_store import CloudArchive, CloudStateError, GitHubStore
from .daily import manifest_path, needed_media, run_daily
from .network import Network
from .publisher import PublicationState
from .state import State, collection_lock
from .telegram_transport import TelegramTransport


@asynccontextmanager
async def telegram_transport():
    try:
        credentials = json.loads(os.environ["COVERPACKS_TELEGRAM"])
        session = StringSession(credentials["session"])
        api_id, api_hash = int(credentials["api_id"]), credentials["api_hash"]
    except (KeyError, ValueError, TypeError):
        raise CloudStateError("Telegram cloud secret is missing or invalid") from None
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    client = TelegramClient(session, api_id, api_hash, request_retries=0, connection_retries=0,
                            flood_sleep_threshold=0, receive_updates=False, timeout=20,
                            device_model="Coverpacks cloud", system_version="Linux",
                            app_version="0.1.0")
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CloudStateError("Telegram cloud session needs authorization")
        yield TelegramTransport(client)
    finally:
        await client.disconnect()


async def probe_source(settings, root):
    async with Network(settings["contact"], root, concurrency=1) as network:
        api = BandcampAPI(network)
        page = await api.discover(fresh=True)
        if not page.items:
            raise CloudStateError("Bandcamp returned an empty discovery page")
        cover = await network.download(await api.artwork(page.items[0]))
        return {"candidates": len(page.items), "cover_width": cover.width,
                "cover_height": cover.height, "cover_bytes": cover.size_bytes}


async def resumable_daily(state, ledger, config, settings, checkpoint):
    """Reconnect after bounded transport failures, reconciling the saved intent.

    Retry the complete daily state machine, never an individual Telegram write.
    A successful durable checkpoint is required before each reconnect.
    """
    for attempt in range(3):
        try:
            return await run_daily(state, ledger, config, settings, telegram_transport, checkpoint)
        except (ConnectionError, TimeoutError) as error:
            await checkpoint()
            if attempt == 2:
                raise
            delay = (5, 15)[attempt]
            print(json.dumps({"status": "transport_retry", "error_type": type(error).__name__,
                              "next_attempt": attempt + 2, "delay_seconds": delay}), flush=True)
            await asyncio.sleep(delay)


async def execute(args):
    store = GitHubStore(args.repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    archive = CloudArchive(args.data_dir, store)
    root = archive.root
    with collection_lock(root):
        if any(root.rglob("*.sqlite3*")):
            raise CloudStateError("Cloud restore requires a fresh data directory; existing local databases were not replaced")
        archive.restore()
        settings = json.loads((root / "daily.json").read_bytes())
        config = json.loads((root / "telegram/channel.json").read_bytes())
        state, ledger = State(root), PublicationState(root / "telegram")
        try:
            manifest = manifest_path(root, settings["prefix"])
            if args.mode == "preflight":
                # Exercise archive transfer and a real asset without any session.
                archive.hydrate([Path(config["avatar"])])
                source = await probe_source(settings, root)
                print(json.dumps({"status": "preflight_ok", "archived_files": len(archive.media),
                                  "packs": len(json.loads(manifest.read_bytes())["packs"]),
                                  "bandcamp": source,
                                  "next_due_at": ledger.get("daily_next_due_at")}))
                return
            if args.mode == "verify":
                archive.hydrate([Path(config["avatar"])])
                async with telegram_transport() as transport:
                    account = await transport.me()
                    if account["id"] != ledger.get("account_id"):
                        raise CloudStateError("Telegram account differs from the existing ledger")
                    if not await transport.verify_channel(ledger.get("channel"), config):
                        raise CloudStateError("Telegram channel no longer matches the saved plan")
                    pack_names = [row[0] for row in ledger.db.execute("SELECT short_name FROM packs WHERE status='published'")]
                    for name in pack_names:
                        if await transport.get_set(name) is None:
                            raise CloudStateError("A published pack is missing")
                print(json.dumps({"status": "telegram_verified", "packs": len(pack_names), "writes": 0}))
                return
            # Most scheduled checks need no network/asset transfer at all.
            import time
            if not ledger.get("daily_active_pack") and time.time() < (ledger.get("daily_next_due_at") or 0):
                print(json.dumps({"status": "not_due", "next_due_at": ledger.get("daily_next_due_at")}))
                return
            archive.hydrate(needed_media(state, ledger, config))
            async def checkpoint():
                archive.checkpoint()
            try:
                result = await resumable_daily(state, ledger, config, settings, checkpoint)
            except Exception:
                # Preserve collection progress and explicit FloodWait. A failed
                # CAS stays fatal; this never retries any Telegram operation.
                try:
                    archive.checkpoint()
                except Exception:
                    pass
                raise
            print(json.dumps(result, ensure_ascii=False))
        finally:
            ledger.close()
            state.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("preflight", "verify", "run"), default="preflight")
    args = parser.parse_args(argv)
    try:
        asyncio.run(execute(args))
        return 0
    except Exception as error:
        # RPC and HTTP details can contain request values; never print them.
        print(f"Cloud run stopped: {type(error).__name__}. Saved history will be reconciled on retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

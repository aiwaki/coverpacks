"""Publication state transitions against a Telegram-shaped in-memory service."""
import asyncio
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from telethon import errors

from coverpacks.emoji import prepare
from coverpacks.publisher import (
    DeferredPublication, PublicationError, PublicationState, Publisher,
    ReconciliationRequired, _pack_posts, _publication_posts, _search_keywords, _units,
    _validate_manifest,
)


CONFIG = {"title": "SLEEVE · обложки на виниле", "about": "Обложки виниловых релизов в Telegram emoji.", "username": "sleeve_test", "avatar": None}


class FakeTelegram:
    def __init__(self):
        self.account = {"id": 123, "premium": True}
        self.channels, self.sets, self.posts, self.events = [], {}, {}, []
        self.scheduled_posts = {}
        self.scheduled_ids = 10000
        self.failures, self.documents = {}, 100
        self.find_posts = True
        self.content_verified = False

    def fail(self, operation, when="before", error=None):
        self.failures[(operation, when)] = error or OSError("response lost")

    def failure(self, operation, when):
        error = self.failures.pop((operation, when), None)
        if error:
            raise error

    async def me(self):
        self.events.append(("me",))
        self.failure("me", "before")
        return dict(self.account)

    async def capabilities(self):
        return {"max_caption_length": 4096 if self.account["premium"] else 1024}

    async def owned_channels(self, title, about):
        self.events.append(("owned_channels",))
        self.failure("owned_channels", "before")
        return copy.deepcopy([channel for channel in self.channels if channel["title"] == title and channel["about"] == about])

    async def create_channel(self, title, about):
        self.events.append(("create_channel",))
        self.failure("create_channel", "before")
        channel = {"id": 10 + len(self.channels), "access_hash": 999, "title": title, "about": about,
                   "username": None, "creator": True, "broadcast": True}
        self.channels.append(channel)
        self.failure("create_channel", "after")
        return dict(channel)

    async def configure_channel(self, channel, username, avatar):
        self.events.append(("configure_channel",))
        self.failure("configure_channel", "before")
        result = dict(channel, username=username)
        for own in self.channels:
            if own["id"] == channel["id"]:
                own["username"] = username
        self.failure("configure_channel", "after")
        return result

    async def verify_channel(self, channel, config):
        self.events.append(("verify_channel", channel["id"]))
        self.failure("verify_channel", "before")
        return any(own["id"] == channel["id"] and own.get("creator") and own.get("broadcast")
                   and all(own.get(key) == config[key] for key in ("title", "about", "username"))
                   for own in self.channels)

    async def upload_emoji(self, path):
        self.events.append(("upload_emoji", str(path)))
        self.failure("upload_emoji", "before")
        self.documents += 1
        result = {"id": self.documents, "access_hash": 1, "file_reference": "00"}
        self.failure("upload_emoji", "after")
        return result

    async def get_set(self, short_name):
        self.events.append(("get_set", short_name))
        self.failure("get_set", "before")
        return copy.deepcopy(self.sets.get(short_name))

    async def verify_set_content(self, remote, pack):
        self.events.append(("verify_set_content", pack["short_name"]))
        self.failure("verify_set_content", "before")
        return self.content_verified

    async def create_set(self, title, short_name, documents, emoji, keywords=None):
        self.events.append(("create_set", short_name, [doc["id"] for doc in documents]))
        self.failure("create_set", "before")
        if short_name in self.sets:
            raise AssertionError("duplicate set creation")
        result = {"id": len(self.sets) + 1, "access_hash": 999, "short_name": short_name, "title": title,
                  "creator": True, "emojis": True, "documents": [dict(doc, alt=emoji, keywords=(keywords[index].split(',') if keywords and keywords[index] else [])) for index,doc in enumerate(documents)]}
        self.sets[short_name] = result
        self.failure("create_set", "after")
        return copy.deepcopy(result)

    async def change_keywords(self, document, keywords):
        self.events.append(("change_keywords", document["id"], keywords))
        self.failure("change_keywords", "before")
        for remote in self.sets.values():
            for doc in remote["documents"]:
                if doc["id"] == document["id"]:
                    doc["keywords"] = keywords.split(',') if keywords else []
                    self.failure("change_keywords", "after")
                    return copy.deepcopy(remote)
        raise AssertionError("unknown document")

    async def set_thumbnail(self, name, document_id):
        self.events.append(("set_thumbnail", name, document_id))
        self.failure("set_thumbnail", "before")
        self.failure("set_thumbnail", "after")

    async def find_post(self, channel, text, photo_path=None):
        self.events.append(("find_post",))
        self.failure("find_post", "before")
        if self.find_posts:
            for post in self.posts.values():
                if post["body"] == text and (photo_path is None or post.get("photo_path") == photo_path):
                    return {"id": post["id"], "link": post["link"]}
        return None

    async def find_scheduled_post(self, channel, text, photo_path=None):
        self.events.append(("find_scheduled_post", text, photo_path))
        self.failure("find_scheduled_post", "before")
        for post in self.scheduled_posts.values():
            if post["body"] == text and (photo_path is None or post.get("photo_path") == photo_path):
                return {"id": post["id"], "scheduled_for": post["scheduled_for"], "scheduled": True}
        return None

    def deliver_scheduled(self, random_ids=None):
        delivered = []
        for random_id in list(self.scheduled_posts) if random_ids is None else random_ids:
            queued = self.scheduled_posts.pop(random_id)
            message_id = 100 + len(self.posts)
            self.posts[random_id] = dict(queued, id=message_id,
                link=f"https://t.me/{queued['username']}/{message_id}")
            delivered.append({"id": message_id, "link": self.posts[random_id]["link"]})
        return delivered

    async def send_post(self, channel, text, random_id, emoji_documents=None, links=None, photo_path=None, *, formatting=None, schedule_at=None):
        self.events.append(("send_post", random_id, text))
        self.failure("send_post", "before")
        if schedule_at is not None and random_id not in self.posts:
            if random_id not in self.scheduled_posts:
                self.scheduled_ids += 1
                self.scheduled_posts[random_id] = {
                    "id": self.scheduled_ids, "scheduled_for": schedule_at, "username": channel["username"],
                    "body": text, "documents": emoji_documents, "links": links,
                    "photo_path": photo_path, "formatting": formatting,
                }
            self.failure("send_post", "after")
            result = self.scheduled_posts[random_id]
            return {"id": result["id"], "scheduled_for": result["scheduled_for"], "scheduled": True}
        if random_id not in self.posts:
            message_id = 100 + len(self.posts)
            self.posts[random_id] = {"id": message_id, "link": f"https://t.me/{channel['username']}/{message_id}",
                                     "body": text, "documents": emoji_documents, "links": links, "photo_path": photo_path,"formatting":formatting}
        self.failure("send_post", "after")
        result = self.posts[random_id]
        return {"id": result["id"], "link": result["link"]}

    async def post_exists(self, channel, message_id):
        self.events.append(("post_exists", message_id))
        self.failure("post_exists", "before")
        return any(post["id"] == message_id for post in self.posts.values())


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "data"
    rows = []
    for seq in range(1, 5):
        image = Image.new("RGB", (120, 120), (seq * 40, 100, 200))
        output = io.BytesIO()
        image.save(output, "PNG")
        content = output.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        path = root / "covers" / f"{digest}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        rows.append({"seq": seq, "status": "downloaded", "excluded": False, "needs_review": False,
                     "path": str(path.relative_to(root)), "sha256": digest, "width": 120, "height": 120,
                     "artist": f"Artist {seq}", "title": f"Album {seq}", "source": "bandcamp",
                     "source_url": f"https://artist{seq}.bandcamp.com/album/a", "entity_id": str(seq)})
    collection = SimpleNamespace(root=root.resolve(), task=lambda task_id: {"id": task_id}, rows=lambda task_id: list(rows))
    path = prepare(collection, 2, pack_size=2)
    state = PublicationState(root / "telegram")
    transport = FakeTelegram()
    publisher = Publisher(state, transport)
    yield SimpleNamespace(path=path, state=state, transport=transport, publisher=publisher, collection=collection, rows=rows)
    state.close()


def run(setup, path=None, config=None):
    return asyncio.run(setup.publisher.run(path or setup.path, config or CONFIG))


def writes(transport):
    return [event for event in transport.events if event[0] in {"create_channel", "configure_channel", "upload_emoji", "create_set", "change_keywords", "set_thumbnail", "send_post"}]


def test_complete_repeat_only_reads_and_packs_publish_in_order(setup):
    report = run(setup)
    assert report["channel_url"] == "https://t.me/sleeve_test"
    assert report["account_id"] == 123
    assert report["welcome_message_id"] == 100
    assert [pack["status"] for pack in report["packs"]] == ["published", "published"]
    assert [pack["message_ids"] for pack in report["packs"]] == [[101], [102]]
    events = setup.transport.events
    sends = [i for i, event in enumerate(events) if event[0] == "send_post"]
    creations = [i for i, event in enumerate(events) if event[0] == "create_set"]
    assert sends[0] < creations[0] < sends[1] < creations[1] < sends[2]
    before = writes(setup.transport)
    assert run(setup) == report
    assert writes(setup.transport) == before
    assert sum(event[0] == "verify_channel" for event in setup.transport.events) == 1
    assert "access_hash" not in json.dumps(report)


def test_empty_channel_description_is_valid_and_preserved(setup):
    config=dict(CONFIG,about="")
    run(setup,config=config)
    assert setup.transport.channels[0]["about"]==""
    assert run(setup,config=config)["packs"][0]["status"]=="published"


def test_photo_and_sources_share_one_post_with_stable_retry_without_generic_tags(setup):
    config = dict(CONFIG, photo_previews=True)
    report = run(setup, config=config)
    posts = list(setup.transport.posts.values())
    assert len(posts) == 3
    assert [bool(post["photo_path"]) for post in posts] == [False, True, True]
    for post in posts[1:]:
        assert '#vinyl' not in post["body"] and '#sleeve_' not in post["body"]
        if post["photo_path"]:
            assert len(post["body"].encode('utf-16-le')) // 2 <= 1024
            assert Path(post["photo_path"]).is_file()
            assert len(post["links"]) == 3 and len(post["documents"]) == 2
            assert [entity['type'] for entity in post['formatting']]==['bold','italic','bold']
            assert 'Artist' in post["body"] and 'Album' in post["body"]
    assert sum(len(post["links"] or []) for post in posts) == 6
    before = writes(setup.transport)
    assert run(setup, config=config) == report
    assert writes(setup.transport) == before


def test_photo_failure_resumes_without_recreating_or_sending_twice(setup):
    config = dict(CONFIG, photo_previews=True, welcome_enabled=False)
    setup.transport.fail('send_post', 'after')
    with pytest.raises(OSError):
        run(setup, config=config)
    assert len(setup.transport.posts) == 1
    first_random = next(iter(setup.transport.posts))
    report = run(setup, config=config)
    assert report['welcome_message_id'] is None
    assert len(setup.transport.posts) == 2
    assert first_random in setup.transport.posts
    assert len(setup.transport.sets) == 2


def test_changed_generated_photo_stops_before_more_writes(setup):
    config = dict(CONFIG, photo_previews=True)
    run(setup, config=config)
    first_photo = next(post['photo_path'] for post in setup.transport.posts.values() if post['photo_path'])
    Path(first_photo).write_bytes(b'changed')
    before = writes(setup.transport)
    with pytest.raises(PublicationError, match='SHA-256'):
        run(setup, config=config)
    assert writes(setup.transport) == before


@pytest.mark.parametrize("change", [
    {"id": 999}, {"creator": False}, {"broadcast": False},
    {"title": "Changed channel"}, {"about": "Changed description"}, {"username": "different_handle"},
])
def test_saved_channel_identity_or_ownership_drift_stops_before_posts(setup, change):
    run(setup)
    before = writes(setup.transport)
    setup.transport.channels[0].update(change)
    setup.transport.events.clear()
    with pytest.raises(ReconciliationRequired, match="не совпадает с планом"):
        run(setup)
    assert [event[0] for event in setup.transport.events] == ["me", "verify_channel"]
    assert writes(setup.transport) == []
    assert len(before) > 0


def test_saved_channel_verification_flood_wait_defers_without_post_requests(setup):
    run(setup)
    setup.transport.events.clear()
    setup.transport.fail("verify_channel", error=errors.FloodWaitError(None, capture=9))
    with pytest.raises(DeferredPublication) as caught:
        run(setup)
    assert caught.value.operation == "verify_channel"
    assert [event[0] for event in setup.transport.events] == ["me", "verify_channel"]
    assert not writes(setup.transport)


def test_lost_channel_response_reconciles_owned_channel_without_second_creation(setup):
    setup.transport.fail("create_channel", "after")
    with pytest.raises(OSError):
        run(setup)
    assert setup.state.get("channel_stage") == "creating"
    assert setup.state.get("channel") is None
    report = run(setup)
    assert report["packs"][0]["status"] == "published"
    assert len(setup.transport.channels) == 1
    assert sum(e[0] == "create_channel" for e in setup.transport.events) == 1


def test_ambiguous_channel_creation_absence_never_blindly_retries(setup):
    setup.transport.fail("create_channel", "before")
    with pytest.raises(OSError):
        run(setup)
    with pytest.raises(ReconciliationRequired, match="Повторное создание остановлено"):
        run(setup)
    assert sum(e[0] == "create_channel" for e in setup.transport.events) == 1
    assert not setup.transport.posts


def test_multiple_matching_owned_channels_stop_without_writes(setup):
    setup.transport.channels = [{"id": n, "access_hash": 1, "title": CONFIG["title"], "about": CONFIG["about"]} for n in (1, 2)]
    with pytest.raises(ReconciliationRequired, match="несколько"):
        run(setup)
    assert not writes(setup.transport)


def test_wrong_account_is_rejected_before_any_new_external_writes(setup):
    run(setup)
    before = writes(setup.transport)
    setup.transport.account["id"] = 999
    with pytest.raises(PublicationError, match="другому Telegram"):
        run(setup)
    assert writes(setup.transport) == before


def test_username_collision_preserves_channel_and_does_not_publish(setup):
    setup.transport.fail("configure_channel", error=errors.UsernameOccupiedError(None))
    with pytest.raises(PublicationError, match="уже занят"):
        run(setup)
    assert len(setup.transport.channels) == 1
    assert not setup.transport.posts and not setup.transport.sets
    assert setup.state.get("channel_configured") is None


def test_rejected_username_can_be_changed_before_any_post_intent(setup):
    setup.transport.fail("configure_channel", error=errors.UsernameOccupiedError(None))
    with pytest.raises(PublicationError, match="уже занят"):
        run(setup)
    changed = dict(CONFIG, username="sleeve_next")
    report = run(setup, config=changed)
    assert report["channel_url"] == "https://t.me/sleeve_next"
    assert len(setup.transport.channels) == 1
    with pytest.raises(PublicationError, match="Настройки канала"):
        run(setup, config=CONFIG)


@pytest.mark.parametrize("error_type", [errors.UserRestrictedError, errors.ChannelsTooMuchError])
def test_known_channel_rejection_does_not_leave_ambiguous_creation(setup, error_type):
    setup.transport.fail("create_channel", error=error_type(None))
    with pytest.raises(error_type):
        run(setup)
    assert setup.state.get("channel_stage") == "planned"
    assert setup.transport.channels == []
    run(setup)
    assert len(setup.transport.channels) == 1


def test_server_error_during_channel_creation_stays_ambiguous(setup):
    setup.transport.fail("create_channel", error=errors.RpcCallFailError(None))
    with pytest.raises(errors.RpcCallFailError):
        run(setup)
    assert setup.state.get("channel_stage") == "creating"
    with pytest.raises(ReconciliationRequired):
        run(setup)
    assert sum(event[0] == "create_channel" for event in setup.transport.events) == 1


def test_lost_create_set_response_reconciles_saved_documents_without_duplicate(setup):
    setup.transport.fail("create_set", "after")
    with pytest.raises(OSError):
        run(setup)
    first = setup.state.db.execute("SELECT * FROM packs ORDER BY number").fetchone()
    assert first["status"] == "creating" and first["documents"]
    run(setup)
    names = [event[1] for event in setup.transport.events if event[0] == "create_set"]
    assert names == ["sleeve_vinyl_001", "sleeve_vinyl_002"]


@pytest.mark.parametrize("verified", [True, False])
def test_server_converted_document_ids_need_content_verification(setup, verified):
    setup.transport.fail("create_set", "after")
    with pytest.raises(OSError):
        run(setup)
    remote = setup.transport.sets["sleeve_vinyl_001"]
    for doc in remote["documents"]:
        doc["id"] += 1000
    setup.transport.content_verified = verified
    if not verified:
        with pytest.raises(ReconciliationRequired):
            run(setup)
        assert len(setup.transport.posts) == 1
        return
    report = run(setup)
    assert report["packs"][0]["status"] == "published"
    saved = setup.state.db.execute("SELECT documents FROM packs WHERE number=1").fetchone()[0]
    assert json.loads(saved) == remote["documents"]
    assert sum(event[0] == "create_set" and event[1] == "sleeve_vinyl_001" for event in setup.transport.events) == 1
    before = writes(setup.transport)
    assert run(setup) == report
    assert writes(setup.transport) == before
    # A later external replacement must not be accepted by image similarity.
    remote["documents"][0]["id"] += 1000
    with pytest.raises(ReconciliationRequired):
        run(setup)
    assert writes(setup.transport) == before


def test_capacity_change_preserves_frozen_packs_and_legacy_series(setup):
    report = run(setup)
    setup.state.put("series", dict(setup.state.get("series"), pack_size=2))
    before = writes(setup.transport)
    document = json.loads(setup.path.read_text())
    document["pack_size"] = 200
    raw = json.dumps(document).encode()
    path = setup.path.parent / f"manifest-{hashlib.sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    assert run(setup, path) == report
    assert writes(setup.transport) == before


@pytest.mark.parametrize("change", ["creator", "emojis", "title", "order", "short_name"])
def test_remote_set_mismatch_blocks_post_and_later_packs(setup, change):
    setup.transport.fail("create_set", "after")
    with pytest.raises(OSError):
        run(setup)
    remote = setup.transport.sets["sleeve_vinyl_001"]
    if change in {"creator", "emojis"}:
        remote[change] = False
    elif change == "order":
        remote["documents"].reverse()
    else:
        remote[change] = "different"
    with pytest.raises(ReconciliationRequired, match="не совпадают"):
        run(setup)
    assert len(setup.transport.posts) == 1  # welcome only
    assert "sleeve_vinyl_002" not in setup.transport.sets


def test_existing_unbound_short_name_cannot_be_adopted_by_appearance(setup):
    setup.transport.sets["sleeve_vinyl_001"] = {"creator": True, "emojis": True, "title": "SLEEVE · 001", "short_name": "sleeve_vinyl_001", "documents": [{"id": 77, "alt": "💿"}]}
    with pytest.raises(ReconciliationRequired):
        run(setup)
    assert not any(event[0] == "upload_emoji" for event in setup.transport.events)


def test_lost_post_response_reuses_exact_persisted_random_id_and_body(setup):
    setup.transport.find_posts = False
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        run(setup)
    saved = dict(setup.state.db.execute("SELECT * FROM posts WHERE key='welcome'").fetchone())
    assert saved["status"] == "sending" and saved["random_id"] > 0
    run(setup)
    sends = [event for event in setup.transport.events if event[0] == "send_post"]
    assert sends[0][1:] == sends[1][1:] == (saved["random_id"], saved["body"])
    assert len(setup.transport.posts) == 3


def test_lost_post_can_be_reconciled_without_another_send(setup):
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        run(setup)
    run(setup)
    assert sum(event[0] == "send_post" for event in setup.transport.events) == 3


def test_deleted_sent_post_stops_and_is_not_recreated(setup):
    run(setup)
    before = writes(setup.transport)
    welcome_random_id = setup.state.db.execute("SELECT random_id FROM posts WHERE key='welcome'").fetchone()[0]
    del setup.transport.posts[welcome_random_id]
    with pytest.raises(ReconciliationRequired, match="отсутствует"):
        run(setup)
    assert writes(setup.transport) == before


def test_deleted_created_set_is_not_recreated(setup):
    run(setup)
    before = writes(setup.transport)
    del setup.transport.sets["sleeve_vinyl_001"]
    with pytest.raises(ReconciliationRequired, match="больше не найден"):
        run(setup)
    assert writes(setup.transport) == before


def test_flood_wait_persists_deadline_no_sleep_and_resumes_after_deadline(setup):
    clock = [1000]
    setup.publisher = Publisher(setup.state, setup.transport, now=lambda: clock[0])
    setup.transport.fail("upload_emoji", error=errors.FloodWaitError(None, capture=17))
    with pytest.raises(DeferredPublication) as caught:
        run(setup)
    assert caught.value.wait_seconds == 17
    assert caught.value.not_before == 1017
    assert setup.state.report()["not_before"] == 1017
    before = list(setup.transport.events)
    with pytest.raises(DeferredPublication):
        run(setup)
    assert setup.transport.events == before
    clock[0] = 1018
    assert all(pack["status"] == "published" for pack in run(setup)["packs"])


def test_explicit_channel_flood_rejection_can_retry_creation_after_wait(setup):
    clock = [1000]
    setup.publisher = Publisher(setup.state, setup.transport, now=lambda: clock[0])
    setup.transport.fail("create_channel", error=errors.FloodWaitError(None, capture=5))
    with pytest.raises(DeferredPublication):
        run(setup)
    assert setup.state.get("channel_stage") == "planned"
    clock[0] = 1006
    run(setup)
    assert len(setup.transport.channels) == 1


def test_expired_file_reference_refreshes_documents_on_next_run(setup):
    setup.transport.fail("create_set", error=errors.FileReferenceExpiredError(None))
    with pytest.raises(PublicationError, match="устаревшие"):
        run(setup)
    assert not setup.transport.sets
    assert setup.state.db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
    assert setup.state.db.execute("SELECT documents FROM packs WHERE number=1").fetchone()[0] is None
    run(setup)
    first_document = setup.transport.sets["sleeve_vinyl_001"]["documents"][0]
    assert first_document["id"] > 102


@pytest.mark.parametrize("asset", ["source_path", "webp_path", "png_path"])
def test_changed_source_or_derivative_fails_before_even_account_lookup(setup, asset):
    document = json.loads(setup.path.read_bytes())
    Path(document["packs"][0]["items"][0][asset]).write_bytes(b"changed")
    with pytest.raises(PublicationError, match="SHA-256"):
        run(setup)
    assert setup.transport.events == []


def test_mutating_frozen_pack_or_configuration_is_rejected(setup):
    run(setup)
    before = list(setup.transport.events)
    with pytest.raises(PublicationError, match="Настройки канала"):
        run(setup, config=dict(CONFIG, title="Another title"))
    document = json.loads(setup.path.read_bytes())
    document["packs"][0]["title"] = "Changed"
    changed = setup.path.parent / "altered.json"
    changed.write_text(json.dumps(document))
    with pytest.raises(PublicationError, match="Замороженный пак"):
        run(setup, path=changed)
    assert setup.transport.events == before


def test_append_new_pack_keeps_previous_remote_packs_and_posts(setup):
    # First publish a prefix of the manifest, then append the already frozen second pack.
    document = json.loads(setup.path.read_bytes())
    first = copy.deepcopy(document)
    first["packs"] = first["packs"][:1]
    first["item_count"] = 2
    first_path = setup.path.parent / "first.json"
    first_path.write_text(json.dumps(first))
    run(setup, path=first_path)
    previous = copy.deepcopy(setup.transport.posts)
    run(setup)
    assert all(setup.transport.posts[key] == value for key, value in previous.items())
    assert len(setup.transport.posts) == 3 and len(setup.transport.sets) == 2
    with pytest.raises(PublicationError, match="удаляет"):
        run(setup, path=first_path)


def test_actual_emoji_alt_and_utf16_source_links_are_correct(setup):
    run(setup)
    post = list(setup.transport.posts.values())[1]
    assert [doc["alt"] for doc in post["documents"]] == ["💿", "💿"]
    assert "💿 💿" in post["body"]
    encoded = post["body"].encode("utf-16-le")
    for index, link in enumerate(post["links"], 1):
        label = encoded[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le")
        assert label == f"Artist {index} — Album {index}"
        assert link["url"] == f"https://artist{index}.bandcamp.com/album/a"


def test_nonpremium_posts_still_publish_packs_with_sources_without_custom_entities(setup):
    setup.transport.account["premium"] = False
    run(setup)
    assert all(not post["documents"] for post in setup.transport.posts.values())
    assert len(setup.transport.sets) == 2


def test_long_unicode_catalogue_splits_under_utf16_limit_and_keeps_all_links():
    items = [{"position": index, "artist": "🎵" * 120, "title": "Длинное название" * 30,
              "source_url": f"https://a.bandcamp.com/album/{index}"} for index in range(1, 201)]
    pack = {"number": 1, "short_name": "long_pack", "items": items}
    documents = [{"id": index, "alt": "🎵"} for index in range(1, 201)]
    posts = _pack_posts(pack, documents, True, "SLEEVE")
    assert len(posts) > 1
    assert sum(len(post["links"]) for post in posts) == 200
    assert sum(len(post["documents"]) for post in posts) == 200
    for post in posts:
        assert len(post["body"].encode("utf-16-le")) // 2 <= 4000
        encoded = post["body"].encode("utf-16-le")
        for link in post["links"]:
            label = encoded[link["offset"] * 2:(link["offset"] + link["length"]) * 2].decode("utf-16-le")
            assert label.endswith("…")
            assert len(label.encode("utf-16-le")) // 2 <= 90


def test_ledger_survives_reopen_after_ambiguous_post(setup):
    setup.transport.find_posts = False
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        run(setup)
    random_id = setup.state.db.execute("SELECT random_id FROM posts WHERE key='welcome'").fetchone()[0]
    path = setup.state.path
    setup.state.close()
    setup.state = PublicationState(path)
    setup.publisher = Publisher(setup.state, setup.transport)
    run(setup)
    assert setup.state.db.execute("SELECT random_id FROM posts WHERE key='welcome'").fetchone()[0] == random_id
    assert len(setup.transport.posts) == 3


def test_photo_pages_preserve_all_names_links_and_matching_covers(monkeypatch,tmp_path):
    items=[{"position":i,"artist":f"Музыкант 🎵{i}","title":"Длинное название альбома "*5,
            "source_url":f"https://a.bandcamp.com/album/{i}"} for i in range(1,201)]
    pack={"number":1,"title":"SLEEVE · 001","short_name":"sleeve_test_001","items":items}
    documents=[{"id":i,"alt":"💿"} for i in range(1,201)]
    captured=[]
    def photos(pack,destination,username,*,page_sizes):
        captured.extend(page_sizes)
        return [{"path":str(tmp_path/f"{i}.jpg"),"sha256":"a"*64} for i in range(len(page_sizes))]
    monkeypatch.setattr("coverpacks.pack_preview.generate_preview",photos)
    posts=_publication_posts(pack,documents,True,"SLEEVE","sleeve_test",tmp_path,caption_limit=1024)
    assert len(posts)>4 and sum(captured)==200 and all(1<=size<=50 for size in captured)
    position=0
    for post,size in zip(posts,captured,strict=True):
        assert post["photo_path"] and _units(post["body"])<=1024
        assert len(post["links"])==size+1 and len(post["documents"])==size
        encoded=post["body"].encode("utf-16-le")
        for link,doc in zip(post["links"][:-1],post["documents"],strict=True):
            item=items[position]
            visible=encoded[link["offset"]*2:(link["offset"]+link["length"])*2].decode("utf-16-le")
            assert visible==f"{item['artist']} — {item['title'].strip()}"
            assert link["url"]==item["source_url"] and doc["id"]==item["position"]
            position+=1
    assert position==200


def test_photo_too_long_single_name_fails_before_rendering(monkeypatch,tmp_path):
    def forbid(*a,**kw):
        pytest.fail("should validate all captions before rendering")
    monkeypatch.setattr("coverpacks.pack_preview.generate_preview",forbid)
    pack={"number":1,"short_name":"sleeve_test_001","items":[
        {"position":1,"artist":"A","title":"🎵"*1000,"source_url":"https://example.com/a"}]}
    with pytest.raises(PublicationError,match="Полное название"):
        _publication_posts(pack,[],False,"SLEEVE","sleeve_test",tmp_path,caption_limit=1024)


def test_photo_uses_observed_account_limit_to_keep_album_together(setup):
    manifest=json.loads(setup.path.read_text());pack=manifest["packs"][0]
    for item in pack["items"]:
        item["title"]="Полное название "*35
    docs=[{"id":i,"alt":"💿"} for i in range(len(pack["items"]))]
    short=_publication_posts(pack,docs,True,"SLEEVE","sleeve_test",setup.state.path.parent/'preview',caption_limit=1024)
    long=_publication_posts(pack,docs,True,"SLEEVE","sleeve_test",setup.state.path.parent/'preview',caption_limit=4096)
    assert len(short)==2 and len(long)==1 and _units(long[0]["body"])>1024


def test_keyword_update_lost_reply_resumes_without_repeating_write(setup):
    run(setup)
    first=setup.transport.sets["sleeve_vinyl_001"]["documents"][0]
    first["keywords"]=[]
    setup.transport.fail("change_keywords","after")
    with pytest.raises(OSError):run(setup)
    changes=[event for event in writes(setup.transport) if event[0]=="change_keywords"]
    assert len(changes)==1
    first["keywords"]=[word.upper() for word in first["keywords"]]
    run(setup)
    assert [event for event in writes(setup.transport) if event[0]=="change_keywords"]==changes


def test_server_trademark_keyword_normalization_does_not_repeat_writes_or_change_visible_titles(setup):
    document = json.loads(setup.path.read_bytes())
    document["packs"][0]["items"][0].update(artist="Dreamcastle™", title="Lego Pirates: 1989-1996")
    setup.path = setup.path.parent / "trademark.json"
    setup.path.write_text(json.dumps(document))
    original_manifest = setup.path.read_bytes()
    create_set = setup.transport.create_set

    async def normalized_create(*args, **kwargs):
        remote = await create_set(*args, **kwargs)
        saved = setup.transport.sets[remote["short_name"]]
        for item in saved["documents"]:
            item["keywords"] = [word.replace("™", "").casefold() for word in item["keywords"]]
        return copy.deepcopy(saved)

    setup.transport.create_set = normalized_create
    first = run(setup)
    assert not [event for event in writes(setup.transport) if event[0] == "change_keywords"]
    before = writes(setup.transport)
    assert run(setup) == first
    assert writes(setup.transport) == before
    assert setup.path.read_bytes() == original_manifest
    assert any("Dreamcastle™ — Lego Pirates: 1989-1996" in post["body"] for post in setup.transport.posts.values())


def test_keyword_update_flood_persists_wait_before_further_posts(setup):
    run(setup)
    setup.transport.sets["sleeve_vinyl_001"]["documents"][0]["keywords"]=[]
    setup.transport.fail("change_keywords",error=errors.FloodWaitError(None,capture=60))
    with pytest.raises(DeferredPublication):run(setup)
    before=list(setup.transport.events)
    with pytest.raises(DeferredPublication):run(setup)
    assert setup.transport.events==before


@pytest.mark.parametrize("item",[
    {"artist":"Peter Gabriel","title":"o\\i"},
    {"artist":"Godspeed You Black Emperor!","title":"Lift Your Skinny Fists Like Antennas To Heaven"},
    {"artist":"A, A","title":"A A"},
    {"artist":"🎵"*100,"title":"💿"*100},
])
def test_search_keywords_fit_service_budget_and_keep_metadata_unchanged(item):
    from coverpacks.telegram_transport import _validated_keywords
    original=dict(item)
    keywords=_search_keywords(item)
    _,words=_validated_keywords(keywords)
    assert words and len(words)<=20 and _units(keywords)<=64
    assert item==original
    if item["artist"]=="Peter Gabriel":assert words==["o\\i","Peter Gabriel"]


DAILY_CONFIG = dict(CONFIG, welcome_enabled=False)


def publish_next(setup, *, path=None, config=None, interval_seconds=86400, schedule_ahead_seconds=0):
    return asyncio.run(setup.publisher.publish_next(
        path or setup.path, config or DAILY_CONFIG, interval_seconds=interval_seconds,
        schedule_ahead_seconds=schedule_ahead_seconds))


def test_daily_publishes_one_pack_per_anchored_interval_and_keeps_queue(setup):
    clock = [1000]
    setup.publisher.now = lambda: clock[0]
    first = publish_next(setup)
    assert first["status"] == "published" and first["pack"] == "sleeve_vinyl_001"
    assert first["next_due_at"] == 87400
    assert [pack["status"] for pack in first["report"]["packs"]] == ["published", "planned"]
    assert setup.state.get("daily_last_completed_at") == 1000
    assert setup.state.get("daily_active_pack") is None
    assert list(setup.transport.sets) == ["sleeve_vinyl_001"]
    before = list(setup.transport.events)
    clock[0] = 87399
    assert publish_next(setup)["status"] == "not_due"
    assert setup.transport.events == before
    clock[0] = 87400
    second = publish_next(setup)
    assert second["status"] == "published" and second["pack"] == "sleeve_vinyl_002"
    assert second["next_due_at"] == 173800
    assert len(setup.transport.posts) == 2


def test_daily_seed_blocks_first_new_pack_until_due(setup):
    setup.publisher.now = lambda: 1000
    setup.state.put("daily_next_due_at", 2000)
    result = publish_next(setup)
    assert result["status"] == "not_due" and result["next_due_at"] == 2000
    assert result["pack"] is None and not setup.transport.events
    assert setup.state.get("daily_active_pack") is None


def test_daily_empty_queue_does_not_consume_a_slot_or_call_telegram(setup):
    run(setup, config=DAILY_CONFIG)
    before = list(setup.transport.events)
    result = publish_next(setup)
    assert result["status"] == "empty" and result["pack"] is None and result["next_due_at"] is None
    assert setup.transport.events == before
    assert setup.state.get("daily_last_completed_at") is None


def test_daily_intent_exists_before_first_external_write(setup):
    setup.publisher.now = lambda: 1000
    setup.state.put("daily_next_due_at", 900)
    original = setup.transport.create_channel

    async def checked_create(title, about):
        active = setup.state.get("daily_active_pack")
        assert active["short_name"] == "sleeve_vinyl_001"
        assert active["manifest_path"] == str(setup.path.resolve())
        assert active["manifest_hash"] == hashlib.sha256(setup.path.read_bytes()).hexdigest()
        assert active["interval_seconds"] == 86400
        assert active["scheduled_for"] == 900
        return await original(title, about)

    setup.transport.create_channel = checked_create
    assert publish_next(setup)["status"] == "published"


@pytest.mark.parametrize("operation", ["create_channel", "create_set", "send_post"])
def test_daily_lost_reply_resumes_same_pack_without_advancing(setup, operation):
    setup.transport.fail(operation, "after")
    with pytest.raises(OSError):
        publish_next(setup)
    active = setup.state.get("daily_active_pack")
    assert active["short_name"] == "sleeve_vinyl_001"
    setup.state.put("daily_next_due_at", 9999999999)
    result = publish_next(setup)
    assert result["status"] == "published" and result["pack"] == active["short_name"]
    assert len(setup.transport.channels) == len(setup.transport.sets) == len(setup.transport.posts) == 1
    assert setup.state.db.execute("SELECT status FROM packs WHERE number=2").fetchone()[0] == "planned"


def test_daily_reopen_resumes_exact_manifest_and_original_interval(setup):
    setup.publisher.now = lambda: 1000
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        publish_next(setup, interval_seconds=60)
    saved = setup.state.get("daily_active_pack")
    future = json.loads(setup.path.read_bytes())
    future["packs"][1]["title"] = "A changed later plan must not replace the active manifest"
    future_path = setup.path.parent / "future.json"
    future_path.write_text(json.dumps(future))
    ledger_path = setup.state.path
    setup.state.close()
    setup.state = PublicationState(ledger_path)
    setup.publisher = Publisher(setup.state, setup.transport, now=lambda: 1000)
    result = publish_next(setup, path=future_path, interval_seconds=1)
    assert result["pack"] == saved["short_name"] and result["next_due_at"] == 1060
    assert len(setup.transport.posts) == 1 and len(setup.transport.sets) == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_daily_completion_crash_only_finishes_bookkeeping_on_retry(setup, monkeypatch, legacy):
    setup.publisher.now = lambda: 1000
    original = setup.publisher._complete_daily

    def interrupted(active):
        raise OSError("crash after confirmed pack, before cadence checkpoint")

    monkeypatch.setattr(setup.publisher, "_complete_daily", interrupted)
    with pytest.raises(OSError):
        publish_next(setup)
    assert setup.state.get("daily_active_pack")["short_name"] == "sleeve_vinyl_001"
    if legacy:
        active = setup.state.get("daily_active_pack")
        del active["scheduled_for"]
        setup.state.put("daily_active_pack", active)
    assert setup.state.db.execute("SELECT status FROM packs WHERE number=1").fetchone()[0] == "published"
    before = list(setup.transport.events)
    monkeypatch.setattr(setup.publisher, "_complete_daily", original)
    setup.publisher.now = lambda: 2000
    result = publish_next(setup)
    assert result["status"] == "published"
    assert result["next_due_at"] == (88400 if legacy else 87400)
    assert setup.transport.events == before
    assert setup.state.get("daily_active_pack") is None
    assert len(setup.transport.sets) == 1


def test_daily_flood_wait_retains_intent_and_defers_without_spending_slot(setup):
    clock = [1000]
    setup.publisher.now = lambda: clock[0]
    setup.transport.fail("create_set", error=errors.FloodWaitError(None, capture=60))
    result = publish_next(setup)
    assert result["status"] == "deferred" and result["not_before"] == 1060
    assert result["operation"] == "create_set:sleeve_vinyl_001"
    assert result["next_due_at"] is None and setup.state.get("daily_last_completed_at") is None
    before = list(setup.transport.events)
    clock[0] = 1059
    assert publish_next(setup)["status"] == "deferred"
    assert setup.transport.events == before
    clock[0] = 1060
    assert publish_next(setup)["status"] == "published"
    assert setup.state.get("daily_next_due_at") == 1000 + 86400
    assert len(setup.transport.sets) == 1


@pytest.mark.parametrize("finished,next_due", [
    (1100, 87400),
    (87400, 173800),
    (1000 + 4 * 86400 + 100, 1000 + 5 * 86400),
])
def test_daily_delayed_completion_keeps_anchor_and_skips_missed_slots(setup, finished, next_due):
    clock = [1050]
    setup.publisher.now = lambda: clock[0]
    setup.state.put("daily_next_due_at", 1000)
    original = setup.transport.send_post

    async def delayed_send(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock[0] = finished
        return result

    setup.transport.send_post = delayed_send
    result = publish_next(setup)
    assert result["status"] == "published" and result["next_due_at"] == next_due
    assert setup.state.get("daily_last_completed_at") == finished
    assert [pack["status"] for pack in result["report"]["packs"]] == ["published", "planned"]
    before = list(setup.transport.events)
    assert publish_next(setup)["status"] == "not_due"
    assert setup.transport.events == before


def test_daily_zero_due_is_a_valid_anchor(setup):
    setup.publisher.now = lambda: 1000
    setup.state.put("daily_next_due_at", 0)
    assert publish_next(setup)["next_due_at"] == 86400


@pytest.mark.parametrize("scheduled_for", [None, True, -1, "1000", float("inf"), float("nan")])
def test_daily_invalid_pinned_slot_fails_before_network_or_state_changes(setup, scheduled_for):
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup)
    active = setup.state.get("daily_active_pack")
    active["scheduled_for"] = scheduled_for
    setup.state.put("daily_active_pack", active)
    before = list(setup.transport.events)
    snapshot = setup.state.db.serialize()
    with pytest.raises(ReconciliationRequired, match="checkpoint"):
        publish_next(setup)
    assert setup.transport.events == before
    assert setup.state.db.serialize() == snapshot


def test_daily_deferral_before_new_intent_does_not_start_work(setup):
    setup.publisher.now = lambda: 1000
    setup.state.put("deferred", {"not_before": 1060, "operation": "account"})
    result = publish_next(setup)
    assert result["status"] == "deferred" and result["not_before"] == 1060
    assert setup.state.get("daily_active_pack") is None and not setup.transport.events


@pytest.mark.parametrize("interval", [0, -1, True, 1.5])
def test_daily_rejects_invalid_interval_before_state_or_network_changes(setup, interval):
    with pytest.raises(PublicationError, match="Интервал"):
        publish_next(setup, interval_seconds=interval)
    assert not setup.transport.events and setup.state.get("config") is None


def test_daily_rejects_changed_pinned_manifest_before_network(setup):
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup)
    before = list(setup.transport.events)
    setup.path.write_bytes(setup.path.read_bytes() + b" ")
    with pytest.raises(PublicationError, match="manifest"):
        publish_next(setup)
    assert setup.transport.events == before


def test_daily_account_mismatch_never_writes_to_telegram(setup):
    setup.state.put("account_id", 999)
    with pytest.raises(PublicationError, match="другому"):
        publish_next(setup)
    assert not writes(setup.transport)


def test_daily_uses_existing_collection_lock(setup):
    from coverpacks.state import collection_lock
    with collection_lock(setup.state.path.parent):
        with pytest.raises(ValueError, match="уже используется"):
            publish_next(setup)
    assert not setup.transport.events and setup.state.get("daily_active_pack") is None


@pytest.mark.parametrize("operation", ["create_channel", "configure_channel", "create_set:", "send_post:"])
def test_checkpoint_failure_prevents_corresponding_external_mutation(setup, operation):
    captured = []

    async def checkpoint():
        actual = setup.state.get("last_operation")
        if actual == operation or actual.startswith(operation):
            captured.append(actual)
            assert setup.state.get("daily_active_pack")["short_name"] == "sleeve_vinyl_001"
            if operation == "create_channel":
                assert setup.state.get("channel_stage") == "creating"
            elif operation == "configure_channel":
                assert setup.state.get("channel")["id"] == 10
            elif operation == "create_set:":
                row = setup.state.db.execute("SELECT status,documents FROM packs WHERE number=1").fetchone()
                assert row["status"] == "creating" and len(json.loads(row["documents"])) == 2
            else:
                row = setup.state.db.execute("SELECT status,random_id FROM posts WHERE pack_name='sleeve_vinyl_001'").fetchone()
                assert row["status"] == "sending" and row["random_id"] > 0
            raise OSError("durable checkpoint unavailable")

    setup.publisher.checkpoint = checkpoint
    with pytest.raises(OSError, match="durable checkpoint"):
        publish_next(setup)
    assert len(captured) == 1
    assert not any(event[0] == operation.rstrip(":") for event in setup.transport.events)


def test_checkpoint_records_confirmed_send_and_failure_resumes_without_duplicate(setup):
    snapshots = []

    async def checkpoint():
        rows = [dict(row) for row in setup.state.db.execute("SELECT * FROM posts ORDER BY part")]
        snapshots.append(rows)
        if rows and rows[0]["status"] == "sent":
            raise OSError("checkpoint acknowledgement lost")

    setup.publisher.checkpoint = checkpoint
    with pytest.raises(OSError, match="acknowledgement lost"):
        publish_next(setup)
    assert len(setup.transport.posts) == 1
    assert snapshots[-2][0]["status"] == "sending"
    assert snapshots[-1][0]["status"] == "sent"
    assert snapshots[-1][0]["message_id"] == 100
    setup.publisher.checkpoint = None
    assert publish_next(setup)["status"] == "published"
    assert len(setup.transport.posts) == 1
    assert sum(event[0] == "send_post" for event in setup.transport.events) == 1


def test_checkpoint_also_saves_reconciled_send_after_lost_reply(setup):
    saved_sent = []

    async def checkpoint():
        saved_sent.extend(dict(row) for row in setup.state.db.execute("SELECT * FROM posts WHERE status='sent'"))

    setup.publisher.checkpoint = checkpoint
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        publish_next(setup)
    assert saved_sent == []
    assert publish_next(setup)["status"] == "published"
    assert [row["message_id"] for row in saved_sent] == [100]
    assert len(setup.transport.posts) == 1


def remove_pack_art(pack):
    for item in pack["items"]:
        for extension in ("source", "webp", "png"):
            Path(item[f"{extension}_path"]).unlink()


def test_daily_can_omit_exact_published_art_but_run_keeps_full_validation(setup):
    setup.publisher.now = lambda: 1000
    assert publish_next(setup)["status"] == "published"
    remove_pack_art(json.loads(setup.path.read_text())["packs"][0])
    setup.transport.events.clear()
    with pytest.raises(PublicationError, match="прочитать файл"):
        run(setup, config=DAILY_CONFIG)
    assert not setup.transport.events
    setup.publisher.now = lambda: 1000 + 86400
    result = publish_next(setup)
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_002"
    assert len(setup.transport.posts) == 2


@pytest.mark.parametrize("change", ["published_hash", "unpublished_status", "unpublished_file"])
def test_daily_archive_never_exempts_changed_or_unpublished_pack(setup, change):
    setup.publisher.now = lambda: 1000
    publish_next(setup)
    document = json.loads(setup.path.read_text())
    if change == "published_hash":
        with setup.state.db:
            setup.state.db.execute("UPDATE packs SET pack_hash=? WHERE number=1", ("0" * 64,))
        remove_pack_art(document["packs"][0])
    elif change == "unpublished_status":
        with setup.state.db:
            setup.state.db.execute("UPDATE packs SET status='created' WHERE number=1")
        remove_pack_art(document["packs"][0])
    else:
        remove_pack_art(document["packs"][1])
    setup.transport.events.clear()
    setup.publisher.now = lambda: 1000 + 86400
    with pytest.raises(PublicationError, match="прочитать файл"):
        publish_next(setup)
    assert not setup.transport.events


@pytest.mark.parametrize("change", ["source_url", "duplicate_source", "relative_path", "order"])
def test_archive_still_validates_structure_and_duplicate_sources(setup, change):
    document = json.loads(setup.path.read_text())
    pack = document["packs"][0]
    if change == "source_url":
        pack["items"][0]["source_url"] = "file:///private"
    elif change == "duplicate_source":
        pack["items"][1]["source_sha256"] = pack["items"][0]["source_sha256"]
    elif change == "relative_path":
        pack["items"][0]["png_path"] = "missing.png"
    else:
        pack["items"][0]["position"] = 2
    digest = hashlib.sha256(json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = setup.path.parent / "archive-structure.json"
    path.write_text(json.dumps(document))
    with pytest.raises(PublicationError):
        _validate_manifest(path, archived_packs={pack["short_name"]: digest})


@pytest.mark.parametrize("ahead", [-1, 86401, True, False, 1.5, "3600", None])
def test_scheduled_ahead_rejects_invalid_window_before_state_or_network_changes(setup, ahead):
    snapshot = setup.state.db.serialize()
    with pytest.raises(PublicationError):
        publish_next(setup, schedule_ahead_seconds=ahead)
    assert setup.transport.events == []
    assert setup.state.db.serialize() == snapshot


def scheduled_case(setup, *, due=2000, now=1000):
    clock = [now]
    setup.publisher.now = lambda: clock[0]
    setup.state.put("daily_next_due_at", due)
    return clock


def test_scheduled_ahead_starts_at_window_boundary_and_retains_active_slot(setup):
    scheduled_case(setup, due=5000)
    result = publish_next(setup, schedule_ahead_seconds=3999)
    assert result["status"] == "not_due" and setup.transport.events == []
    assert setup.state.get("daily_active_pack") is None
    result = publish_next(setup, schedule_ahead_seconds=4000)
    assert result["status"] == "scheduled" and result["pack"] == "sleeve_vinyl_001"
    active = setup.state.get("daily_active_pack")
    assert active["scheduled_for"] == active["delivery_at"] == 5000
    assert active["interval_seconds"] == 86400
    assert setup.state.get("daily_last_completed_at") is None
    assert setup.state.get("daily_next_due_at") == 5000
    assert [pack["status"] for pack in result["report"]["packs"]] == ["scheduled", "planned"]
    assert result["report"]["packs"][0]["message_ids"] == []
    assert result["report"]["packs"][0]["links"] == []
    assert len(setup.transport.scheduled_posts) == 1 and setup.transport.posts == {}
    post = setup.state.db.execute("SELECT * FROM posts WHERE pack_name='sleeve_vinyl_001'").fetchone()
    remote = next(iter(setup.transport.scheduled_posts.values()))
    assert post["status"] == "scheduled" and post["schedule_at"] == 5000
    assert post["scheduled_message_id"] == remote["id"]
    assert post["message_id"] is None and post["url"] is None


@pytest.mark.parametrize("due,now,interval,delivery", [
    (2000, 1000, 1000, 2000),
    (1900, 1000, 100, 2000),
    (1060, 1000, 1000, 2060),
    (900, 1000, 1000, 2900),
    (900, 5000, 1000, 6900),
])
def test_scheduled_delivery_uses_first_anchor_slot_beyond_preparation_margin(setup, due, now, interval, delivery):
    scheduled_case(setup, due=due, now=now)
    result = publish_next(setup, interval_seconds=interval, schedule_ahead_seconds=86400)
    active = setup.state.get("daily_active_pack")
    assert result["status"] == "scheduled"
    assert active["scheduled_for"] == due and active["delivery_at"] == delivery
    assert delivery > now + 900 and (delivery - due) % interval == 0
    assert next(iter(setup.transport.scheduled_posts.values()))["scheduled_for"] == delivery
    assert setup.transport.posts == {}


def test_scheduled_intent_pins_delivery_and_mode_across_restart_and_option_changes(setup):
    scheduled_case(setup)
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup, interval_seconds=1000, schedule_ahead_seconds=1000)
    original = copy.deepcopy(setup.state.get("daily_active_pack"))
    assert original["scheduled_for"] == original["delivery_at"] == 2000
    future = json.loads(setup.path.read_bytes())
    future["packs"][1]["title"] = "Changed future plan"
    future_path = setup.path.parent / "another-plan.json"
    future_path.write_text(json.dumps(future))
    ledger_path = setup.state.path
    setup.state.close()
    setup.state = PublicationState(ledger_path)
    # The same delivery is now inside the 15-minute preparation margin, but
    # remains safely beyond the 60-second send cutoff and must not move.
    setup.publisher = Publisher(setup.state, setup.transport, now=lambda: 1500)
    result = publish_next(setup, path=future_path, interval_seconds=10, schedule_ahead_seconds=0)
    assert result["status"] == "scheduled" and result["pack"] == original["short_name"]
    assert setup.state.get("daily_active_pack") == original
    assert next(iter(setup.transport.scheduled_posts.values()))["scheduled_for"] == 2000
    assert setup.transport.posts == {} and len(setup.transport.sets) == 1


def test_scheduled_lost_send_response_reconciles_queue_and_checkpoints_without_resend(setup):
    scheduled_case(setup)
    setup.transport.fail("send_post", "after")
    with pytest.raises(OSError):
        publish_next(setup, schedule_ahead_seconds=3600)
    first = setup.state.db.execute("SELECT * FROM posts").fetchone()
    assert first["status"] == "sending" and first["schedule_at"] == 2000
    assert len(setup.transport.scheduled_posts) == 1 and setup.transport.posts == {}
    writes_before = writes(setup.transport)
    saved = []
    async def checkpoint():
        saved.extend(dict(row) for row in setup.state.db.execute("SELECT * FROM posts WHERE status='scheduled'"))
    setup.publisher.checkpoint = checkpoint
    result = publish_next(setup, schedule_ahead_seconds=0)
    assert result["status"] == "scheduled"
    assert writes(setup.transport) == writes_before
    remote = next(iter(setup.transport.scheduled_posts.values()))
    assert saved and saved[-1]["scheduled_message_id"] == remote["id"]
    assert saved[-1]["message_id"] is None
    assert setup.state.get("daily_active_pack") is not None


def test_scheduled_repeat_checks_history_before_queue_and_never_writes_again(setup):
    clock = scheduled_case(setup)
    publish_next(setup, schedule_ahead_seconds=3600)
    before = writes(setup.transport)
    setup.transport.events.clear()
    # A queue item may still be visible while its due-time delivery is pending.
    clock[0] = 2001
    result = publish_next(setup, schedule_ahead_seconds=0)
    assert result["status"] == "scheduled"
    assert writes(setup.transport) == []
    operations = [event[0] for event in setup.transport.events]
    assert operations.index("find_post") < operations.index("find_scheduled_post")
    assert len(setup.transport.scheduled_posts) == 1 and setup.transport.posts == {}
    assert setup.state.get("daily_last_completed_at") is None
    assert setup.state.get("daily_active_pack")["delivery_at"] == 2000
    assert len([event for event in before if event[0] == "send_post"]) == 1


def test_scheduled_delivery_reconciles_new_history_ids_before_completing_cadence(setup):
    clock = scheduled_case(setup)
    publish_next(setup, interval_seconds=3600, schedule_ahead_seconds=3600)
    queued_id = next(iter(setup.transport.scheduled_posts.values()))["id"]
    delivered = setup.transport.deliver_scheduled()
    assert delivered[0]["id"] != queued_id
    setup.transport.events.clear()
    clock[0] = 2010
    result = publish_next(setup, schedule_ahead_seconds=0)
    assert result["status"] == "published" and result["pack"] == "sleeve_vinyl_001"
    assert result["next_due_at"] == 5600
    assert result["report"]["packs"][0]["message_ids"] == [delivered[0]["id"]]
    assert result["report"]["packs"][0]["links"] == [delivered[0]["link"]]
    post = setup.state.db.execute("SELECT * FROM posts").fetchone()
    assert post["status"] == "sent" and post["message_id"] == delivered[0]["id"]
    assert setup.state.get("daily_active_pack") is None
    assert setup.state.get("daily_last_completed_at") == 2010
    assert setup.state.db.execute("SELECT status FROM packs WHERE number=2").fetchone()[0] == "planned"
    assert not any(event[0] == "find_scheduled_post" for event in setup.transport.events)
    assert writes(setup.transport) == []


@pytest.mark.parametrize("now", [1100, 2100])
def test_confirmed_scheduled_post_missing_from_queue_and_history_fails_closed(setup, now):
    clock = scheduled_case(setup)
    publish_next(setup, schedule_ahead_seconds=3600)
    setup.transport.scheduled_posts.clear()
    setup.transport.events.clear()
    clock[0] = now
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert writes(setup.transport) == [] and setup.transport.posts == {}
    assert setup.state.get("daily_active_pack") is not None
    assert setup.state.db.execute("SELECT status FROM posts").fetchone()[0] == "scheduled"


@pytest.mark.parametrize("lost_response", [False, True])
def test_scheduled_queue_date_drift_fails_without_resend(setup, lost_response):
    scheduled_case(setup)
    if lost_response:
        setup.transport.fail("send_post", "after")
        with pytest.raises(OSError):
            publish_next(setup, schedule_ahead_seconds=3600)
    else:
        publish_next(setup, schedule_ahead_seconds=3600)
    next(iter(setup.transport.scheduled_posts.values()))["scheduled_for"] += 1
    setup.transport.events.clear()
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert writes(setup.transport) == [] and setup.transport.posts == {}
    assert setup.state.get("daily_active_pack")["delivery_at"] == 2000


def test_confirmed_scheduled_queue_id_replacement_fails_without_resend(setup):
    scheduled_case(setup)
    publish_next(setup, schedule_ahead_seconds=3600)
    recorded = setup.state.db.execute("SELECT scheduled_message_id FROM posts").fetchone()[0]
    next(iter(setup.transport.scheduled_posts.values()))["id"] += 1
    setup.transport.events.clear()
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert writes(setup.transport) == []
    assert setup.state.db.execute("SELECT scheduled_message_id FROM posts").fetchone()[0] == recorded


@pytest.mark.parametrize("retry_now,can_schedule", [(1100, True), (1940, False)])
def test_partial_scheduled_pack_retries_only_planned_posts_and_never_falls_back_immediate(setup, monkeypatch, retry_now, can_schedule):
    clock = scheduled_case(setup)
    monkeypatch.setattr("coverpacks.publisher._pack_posts", lambda *_args: [
        {"body": "Scheduled part 1", "documents": [], "links": []},
        {"body": "Scheduled part 2", "documents": [], "links": []},
    ])
    async def interrupted_checkpoint():
        rows = list(setup.state.db.execute("SELECT status FROM posts ORDER BY part"))
        if [row[0] for row in rows] == ["scheduled", "planned"]:
            raise OSError("first part queued, second part still planned")
    setup.publisher.checkpoint = interrupted_checkpoint
    with pytest.raises(OSError, match="second part still planned"):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert len(setup.transport.scheduled_posts) == 1
    first_random_id = next(iter(setup.transport.scheduled_posts))
    setup.publisher.checkpoint = None
    clock[0] = retry_now
    if can_schedule:
        result = publish_next(setup, schedule_ahead_seconds=0)
        assert result["status"] == "scheduled"
        assert len(setup.transport.scheduled_posts) == 2
        assert [row[0] for row in setup.state.db.execute("SELECT status FROM posts ORDER BY part")] == ["scheduled", "scheduled"]
    else:
        with pytest.raises(ReconciliationRequired):
            publish_next(setup, schedule_ahead_seconds=0)
        assert len(setup.transport.scheduled_posts) == 1
    sends = [event for event in setup.transport.events if event[0] == "send_post"]
    assert sum(event[1] == first_random_id for event in sends) == 1
    assert len(sends) == (2 if can_schedule else 1)
    assert setup.transport.posts == {} and len(setup.transport.sets) == 1
    assert setup.state.get("daily_active_pack")["delivery_at"] == 2000


@pytest.mark.parametrize("retry_now,can_schedule", [(1939, True), (1940, False), (2001, False)])
def test_scheduled_sending_without_remote_copy_obeys_pinned_sixty_second_cutoff(setup, retry_now, can_schedule):
    clock = scheduled_case(setup)
    setup.transport.fail("send_post", "before")
    with pytest.raises(OSError):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert setup.state.db.execute("SELECT status FROM posts").fetchone()[0] == "sending"
    assert setup.transport.scheduled_posts == {}
    setup.transport.events.clear()
    clock[0] = retry_now
    if can_schedule:
        assert publish_next(setup, schedule_ahead_seconds=0)["status"] == "scheduled"
        assert len(setup.transport.scheduled_posts) == 1
    else:
        with pytest.raises(ReconciliationRequired):
            publish_next(setup, schedule_ahead_seconds=0)
        assert writes(setup.transport) == [] and setup.transport.scheduled_posts == {}
    assert setup.transport.posts == {}
    assert setup.state.get("daily_active_pack")["delivery_at"] == 2000


@pytest.mark.parametrize("anchor_present", [False, True])
def test_legacy_active_without_delivery_at_remains_immediate_when_scheduling_enabled(setup, anchor_present):
    setup.publisher.now = lambda: 1000
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup)
    active = setup.state.get("daily_active_pack")
    assert "delivery_at" not in active
    if not anchor_present:
        active.pop("scheduled_for", None)
        setup.state.put("daily_active_pack", active)
    result = publish_next(setup, schedule_ahead_seconds=86400)
    assert result["status"] == "published"
    assert len(setup.transport.posts) == 1 and setup.transport.scheduled_posts == {}
    assert setup.state.db.execute("SELECT schedule_at FROM posts").fetchone()[0] is None
    assert not any(event[0] == "find_scheduled_post" for event in setup.transport.events)


def test_scheduled_photo_reconciliation_passes_photo_path_to_both_lookups(setup):
    scheduled_case(setup)
    config = dict(DAILY_CONFIG, photo_previews=True)
    seen_history_photos = []
    original_find = setup.transport.find_post
    async def find_with_photo(channel, text, photo_path=None):
        seen_history_photos.append(photo_path)
        return await original_find(channel, text, photo_path)
    setup.transport.find_post = find_with_photo
    assert publish_next(setup, config=config, schedule_ahead_seconds=3600)["status"] == "scheduled"
    queued = next(iter(setup.transport.scheduled_posts.values()))
    assert queued["photo_path"] and Path(queued["photo_path"]).is_file()
    seen_history_photos.clear()
    setup.transport.events.clear()
    assert publish_next(setup, config=config, schedule_ahead_seconds=0)["status"] == "scheduled"
    assert seen_history_photos == [queued["photo_path"]]
    lookups = [event for event in setup.transport.events if event[0] == "find_scheduled_post"]
    assert len(lookups) == 1 and lookups[0][2] == queued["photo_path"]
    assert writes(setup.transport) == []


@pytest.mark.parametrize("delivery", [None, True, False, 0, -1, 2000.0, "2000", float("inf"), float("nan")])
def test_scheduled_corrupt_active_delivery_fails_before_network_or_state_changes(setup, delivery):
    scheduled_case(setup)
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup, schedule_ahead_seconds=3600)
    active = setup.state.get("daily_active_pack")
    active["delivery_at"] = delivery
    setup.state.put("daily_active_pack", active)
    setup.transport.events.clear()
    snapshot = setup.state.db.serialize()
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=0)
    assert setup.transport.events == []
    assert setup.state.db.serialize() == snapshot


def test_scheduled_delivery_without_pinned_anchor_fails_before_network(setup):
    scheduled_case(setup)
    setup.transport.fail("me")
    with pytest.raises(OSError):
        publish_next(setup, schedule_ahead_seconds=3600)
    active = setup.state.get("daily_active_pack")
    del active["scheduled_for"]
    setup.state.put("daily_active_pack", active)
    setup.transport.events.clear()
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=0)
    assert setup.transport.events == []


@pytest.mark.parametrize("scheduled,changed_date", [(True, None), (True, 2001), (False, 2000)])
def test_scheduled_post_date_drift_from_active_intent_fails_before_context(setup, scheduled, changed_date):
    if scheduled:
        scheduled_case(setup)
        publish_next(setup, schedule_ahead_seconds=3600)
    else:
        setup.publisher.now = lambda: 1000
        setup.transport.fail("send_post", "before")
        with pytest.raises(OSError):
            publish_next(setup)
    with setup.state.db:
        setup.state.db.execute("UPDATE posts SET schedule_at=?", (changed_date,))
    setup.transport.events.clear()
    snapshot = setup.state.db.serialize()
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=0)
    assert setup.transport.events == []
    assert setup.state.db.serialize() == snapshot


def test_manual_run_respects_queued_first_pack_without_creating_the_next_one(setup):
    scheduled_case(setup)
    publish_next(setup, schedule_ahead_seconds=3600)
    active = copy.deepcopy(setup.state.get("daily_active_pack"))
    setup.transport.events.clear()
    report = run(setup, config=DAILY_CONFIG)
    assert [pack["status"] for pack in report["packs"]] == ["scheduled", "planned"]
    assert list(setup.transport.sets) == ["sleeve_vinyl_001"]
    assert len(setup.transport.scheduled_posts) == 1 and setup.transport.posts == {}
    assert writes(setup.transport) == []
    assert setup.state.get("daily_active_pack") == active


def test_scheduled_send_rechecks_cutoff_after_slow_checkpoint_before_transport(setup):
    clock = scheduled_case(setup)
    send_checkpoints = []
    async def slow_checkpoint():
        operation = setup.state.get("last_operation") or ""
        if operation.startswith("send_post:"):
            post = setup.state.db.execute("SELECT * FROM posts").fetchone()
            send_checkpoints.append((post["status"], post["schedule_at"]))
            clock[0] = 1940  # Exactly delivery_at - 60: sending now could become immediate.
    setup.publisher.checkpoint = slow_checkpoint
    with pytest.raises(ReconciliationRequired):
        publish_next(setup, schedule_ahead_seconds=3600)
    assert send_checkpoints == [("sending", 2000)]
    assert not any(event[0] == "send_post" for event in setup.transport.events)
    assert setup.transport.posts == setup.transport.scheduled_posts == {}
    assert setup.state.db.execute("SELECT status FROM posts").fetchone()[0] == "sending"
    assert setup.state.get("daily_active_pack")["delivery_at"] == 2000


def test_scheduled_pack_completes_only_after_every_queued_part_reaches_history(setup, monkeypatch):
    clock = scheduled_case(setup)
    monkeypatch.setattr("coverpacks.publisher._pack_posts", lambda *_args: [
        {"body": "Scheduled part 1", "documents": [], "links": []},
        {"body": "Scheduled part 2", "documents": [], "links": []},
    ])
    assert publish_next(setup, schedule_ahead_seconds=3600)["status"] == "scheduled"
    random_ids = list(setup.transport.scheduled_posts)
    first = setup.transport.deliver_scheduled([random_ids[0]])[0]
    setup.transport.events.clear()
    clock[0] = 2010
    partial = publish_next(setup, schedule_ahead_seconds=0)
    assert partial["status"] == "scheduled"
    assert [row[0] for row in setup.state.db.execute("SELECT status FROM posts ORDER BY part")] == ["sent", "scheduled"]
    assert setup.state.get("daily_active_pack") is not None
    assert setup.state.get("daily_last_completed_at") is None
    assert setup.state.db.execute("SELECT status FROM packs WHERE number=1").fetchone()[0] == "scheduled"
    second = setup.transport.deliver_scheduled([random_ids[1]])[0]
    complete = publish_next(setup, schedule_ahead_seconds=0)
    assert complete["status"] == "published"
    assert complete["report"]["packs"][0]["message_ids"] == [first["id"], second["id"]]
    assert complete["report"]["packs"][0]["links"] == [first["link"], second["link"]]
    assert setup.state.get("daily_active_pack") is None
    assert writes(setup.transport) == []

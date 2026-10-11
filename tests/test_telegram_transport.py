"""Offline requests against real Telethon constructors; never connect an account."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont
import pytest
from telethon import errors, functions, types

from coverpacks.telegram_transport import TelegramResultError, TelegramTransport, _rgb_rmse, _visible_rgb


NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)
CHANNEL = {"id": 123, "access_hash": 456, "title": "SLEEVE", "username": "sleeve_test"}
DOCUMENT = {"id": 7, "access_hash": 8, "file_reference": "090a"}


def channel(**changes):
    values = {"id": CHANNEL["id"], "access_hash": CHANNEL["access_hash"],
              "title": "SLEEVE", "username": "sleeve_test", "creator": True,
              "broadcast": True, "photo": types.ChatPhotoEmpty(), "date": NOW}
    return types.Channel(**(values | changes))


def document(*, custom=True, **changes):
    attributes = ([types.DocumentAttributeCustomEmoji(
        alt="💿", stickerset=types.InputStickerSetShortName("sleeve_001"))] if custom else [])
    values = {"id": 7, "access_hash": 8, "file_reference": b"\x09\x0a", "date": NOW,
              "mime_type": "image/webp", "size": 500, "dc_id": 2, "attributes": attributes}
    return types.Document(**(values | changes))


def sticker_set(**changes):
    values = {"id": 70, "access_hash": 80, "title": "SLEEVE / 001", "short_name": "sleeve_001",
              "count": 1, "hash": 90, "emojis": True, "creator": True}
    return types.messages.StickerSet(set=types.StickerSet(**(values | changes)),
                                    packs=[], keywords=[], documents=[document()])


def message(text="post", **changes):
    values = {"id": 42, "peer_id": types.PeerChannel(CHANNEL["id"]), "date": NOW,
              "message": text, "post": True}
    return types.Message(**(values | changes))


class FakeClient:
    def __init__(self, responses=(), *, dialogs=(), messages=(), me=None):
        self.responses = deque(responses)
        self.calls = []
        self.uploads = []
        self.dialogs = dialogs
        self.messages = messages
        self.iter_message_calls = []
        self.user = me or SimpleNamespace(id=99, username="owner", premium=True, bot=False)
        self.message_by_id = None

    async def __call__(self, request):
        self.calls.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    async def get_me(self):
        return self.user

    async def upload_file(self, path):
        self.uploads.append(path)
        return types.InputFile(id=1, parts=1, name="upload.webp", md5_checksum="unused")

    async def iter_dialogs(self):
        for entity in self.dialogs:
            yield SimpleNamespace(entity=entity)

    async def iter_messages(self, peer, limit):
        self.iter_message_calls.append((peer, limit))
        for value in self.messages:
            yield value

    async def get_messages(self, peer, ids):
        if isinstance(self.message_by_id, Exception):
            raise self.message_by_id
        return self.message_by_id


def run(method):
    return asyncio.run(method)


class DownloadClient(FakeClient):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = deque(payloads)
        self.downloads = []
        self.maximum_buffer_size = 0

    async def download_file(self, location, *, file, part_size_kb):
        self.downloads.append((location, part_size_kb))
        payload = self.payloads.popleft()
        if isinstance(payload, Exception):
            raise payload
        for offset in range(0, len(payload), part_size_kb * 1024):
            file.write(payload[offset:offset + part_size_kb * 1024])
            self.maximum_buffer_size = max(self.maximum_buffer_size, file.tell())


def emoji_bytes(color=(80, 90, 100, 255), *, size=(100, 100), format="WEBP", **options):
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format=format, lossless=True, **options)
    return buffer.getvalue()


def content_verification_pack(tmp_path, contents):
    items, documents = [], []
    for index, content in enumerate(contents):
        path = tmp_path / f"{index}.webp"
        path.write_bytes(content)
        items.append({"webp_path": str(path), "webp_sha256": hashlib.sha256(content).hexdigest()})
        documents.append(DOCUMENT | {"id": 100 + index, "file_reference": f"0{index % 10}"})
    return {"documents": documents}, {"items": items}


def test_verify_content_matches_ordered_images_after_ids_change(tmp_path):
    contents = [emoji_bytes((220, 30, 50, 255)), emoji_bytes((20, 190, 230, 255))]
    remote, pack = content_verification_pack(tmp_path, contents)
    client = DownloadClient(contents)
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is True
    assert not client.calls and not client.uploads
    assert len(client.downloads) == 2
    for index, (location, part_size) in enumerate(client.downloads):
        assert isinstance(location, types.InputDocumentFileLocation)
        assert location.id == 100 + index and location.access_hash == 8
        assert location.file_reference == bytes.fromhex(f"0{index}")
        assert location.thumb_size == "" and part_size == 64


def test_verify_content_accepts_lossy_server_conversion(tmp_path):
    images = []
    for shift in (0, 100):
        image = Image.new("RGB", (100, 100))
        image.putdata([(30 + shift + x // 3, 40 + y // 4, 80 + shift)
                       for y in range(100) for x in range(100)])
        original, converted = BytesIO(), BytesIO()
        image.save(original, format="WEBP", lossless=True)
        image.save(converted, format="WEBP", quality=35)
        assert original.getvalue() != converted.getvalue()
        images.append((original.getvalue(), converted.getvalue()))
    remote, pack = content_verification_pack(tmp_path, [pair[0] for pair in images])
    client = DownloadClient([pair[1] for pair in images])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is True


def padded_emoji_bytes(color, *, hidden=(0, 0, 0), alpha=255, lossy=False):
    image = Image.new("RGBA", (100, 100), (*hidden, 0))
    image.paste(Image.new("RGBA", (100, 90), (*color, alpha)), (0, 5))
    output = BytesIO()
    image.save(output, "WEBP", lossless=not lossy, quality=75, exact=True)
    return output.getvalue()


def test_verify_content_ignores_changed_rgb_under_exact_zero_alpha(tmp_path):
    original = padded_emoji_bytes((255, 255, 255))
    converted = padded_emoji_bytes((255, 255, 255), hidden=(255, 255, 255))
    assert Image.open(BytesIO(original)).getpixel((0, 0)) != Image.open(BytesIO(converted)).getpixel((0, 0))
    remote, pack = content_verification_pack(tmp_path, [original])
    assert run(TelegramTransport(DownloadClient([converted])).verify_set_content(remote, pack)) is True


def test_verify_content_accepts_lossy_cover_with_transparent_padding(tmp_path):
    original = padded_emoji_bytes((255, 255, 255))
    converted = padded_emoji_bytes((255, 255, 255), lossy=True)
    remote, pack = content_verification_pack(tmp_path, [original])
    assert run(TelegramTransport(DownloadClient([converted])).verify_set_content(remote, pack)) is True


@pytest.mark.parametrize("change", ["visible_color", "alpha"])
def test_verify_content_still_rejects_changed_visible_color_or_alpha_with_padding(tmp_path, change):
    original = padded_emoji_bytes((255, 255, 255))
    converted = padded_emoji_bytes((0, 0, 0) if change == "visible_color" else (255, 255, 255),
                                    hidden=(255, 255, 255), alpha=254 if change == "alpha" else 255)
    remote, pack = content_verification_pack(tmp_path, [original])
    assert run(TelegramTransport(DownloadClient([converted])).verify_set_content(remote, pack)) is False


def test_verify_content_rejects_reordered_covers_with_transparent_padding(tmp_path):
    originals = [padded_emoji_bytes((255, 255, 255)), padded_emoji_bytes((100, 20, 30))]
    remote, pack = content_verification_pack(tmp_path, originals)
    assert run(TelegramTransport(DownloadClient(originals[::-1])).verify_set_content(remote, pack)) is False


def test_verify_content_hidden_rgb_cannot_disambiguate_visually_identical_sources(tmp_path):
    originals = [padded_emoji_bytes((255, 255, 255)),
                 padded_emoji_bytes((255, 255, 255), hidden=(255, 255, 255))]
    remote, pack = content_verification_pack(tmp_path, originals)
    assert run(TelegramTransport(DownloadClient(originals)).verify_set_content(remote, pack)) is False


@pytest.mark.parametrize("failure", ["color_distance", "lowpass_distance", "ambiguous_source", "alpha_mismatch", "invalid_remote_image"])
def test_verification_diagnostics_are_structured_and_exclude_private_references(tmp_path, caplog, failure):
    other = 125 if failure == "ambiguous_source" else 150
    contents = [emoji_bytes((100, 100, 100, 255)), emoji_bytes((other, other, other, 255))]
    remote, pack = content_verification_pack(tmp_path, contents)
    remote["documents"][0].update(id=987654321001, access_hash=987654321002, file_reference="736563726574726566")
    payload = {
        "color_distance": emoji_bytes((135, 135, 135, 255)),
        "lowpass_distance": emoji_bytes((113, 113, 113, 255)),
        "ambiguous_source": emoji_bytes((110, 110, 110, 255)),
        "alpha_mismatch": emoji_bytes((100, 100, 100, 254)),
        "invalid_remote_image": emoji_bytes(size=(512, 512)),
    }[failure]
    assert run(TelegramTransport(DownloadClient([payload])).verify_set_content(remote, pack)) is False
    record = json.loads(caplog.records[-1].getMessage())
    assert record["event"] == "emoji_content_verification_failed"
    assert record["reason"] == failure and record["position"] == 1
    if failure in {"color_distance", "lowpass_distance", "ambiguous_source"}:
        assert record["nearest_other_position"] == 2
        distance = {"color_distance": 35, "lowpass_distance": 13, "ambiguous_source": 10}[failure]
        assert record["own_distance"] == record["lowpass_distance"] == distance
        assert record["nearest_other_distance"] == other - 100 - distance
        assert record["maximum_own_distance"] == 32 and record["maximum_lowpass_distance"] == 12
    elif failure == "alpha_mismatch":
        assert record["mismatch_pixels"] == 10000 and record["maximum_alpha_difference"] == 1
    else:
        assert record["actual"]["width"] == 512 and record["expected_width"] == 100
    for private in ("987654321001", "987654321002", "736563726574726566", str(tmp_path),
                    pack["items"][0]["webp_sha256"]):
        assert private not in caplog.text


@pytest.mark.parametrize("distance,expected", [(12, True), (13, False)])
def test_verify_content_has_stricter_lowpass_bound_for_broad_color_changes(tmp_path, distance, expected):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes((100, 100, 100, 255))])
    client = DownloadClient([emoji_bytes((100 + distance,) * 3 + (255,))])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is expected


@pytest.mark.parametrize("distance,expected", [(32, True), (33, False)])
def test_verify_content_full_resolution_bound_remains_required_when_lowpass_matches(tmp_path, distance, expected):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes((100, 100, 100, 255))])
    converted = Image.new("RGB", (100, 100))
    converted.putdata([(100 + (distance if (x + y) % 2 else -distance),) * 3
                       for y in range(100) for x in range(100)])
    buffer = BytesIO()
    converted.save(buffer, "WEBP", lossless=True)
    assert run(TelegramTransport(DownloadClient([buffer.getvalue()])).verify_set_content(remote, pack)) is expected


def test_verify_content_accepts_chroma_loss_in_saturated_text_but_rejects_wrong_layout(tmp_path):
    source = Image.new("RGBA", (100, 100), (0, 255, 255, 255))
    draw = ImageDraw.Draw(source)
    for y in (10, 34):
        draw.text((0, y), "RED MIX", font=ImageFont.load_default(size=20), fill=(255, 0, 0, 255))
    original, converted = BytesIO(), BytesIO()
    source.save(original, "WEBP", lossless=True)
    source.save(converted, "WEBP", quality=85)
    actual = Image.open(BytesIO(converted.getvalue())).convert("RGBA")
    assert 24 < _rgb_rmse(_visible_rgb(actual), _visible_rgb(source)) <= 32
    remote, pack = content_verification_pack(tmp_path, [original.getvalue()])
    assert run(TelegramTransport(DownloadClient([converted.getvalue()])).verify_set_content(remote, pack)) is True
    wrong = BytesIO()
    source.transpose(Image.Transpose.ROTATE_180).save(wrong, "WEBP", quality=85)
    assert run(TelegramTransport(DownloadClient([wrong.getvalue()])).verify_set_content(remote, pack)) is False


def test_verify_content_rejects_swapped_items_even_when_both_are_close(tmp_path):
    contents = [emoji_bytes((100, 100, 100, 255)), emoji_bytes((120, 120, 120, 255))]
    remote, pack = content_verification_pack(tmp_path, contents)
    client = DownloadClient(list(reversed(contents)))
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False
    assert len(client.downloads) == 1


@pytest.mark.parametrize("other,converted", [(100, 100), (104, 100), (125, 110), (110, 102)])
def test_verify_content_rejects_duplicates_or_ambiguous_nearest_source(tmp_path, other, converted):
    contents = [emoji_bytes((100, 100, 100, 255)), emoji_bytes((other,) * 3 + (255,))]
    remote, pack = content_verification_pack(tmp_path, contents)
    client = DownloadClient([emoji_bytes((converted,) * 3 + (255,)), contents[1]])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False


def test_verify_content_preserves_alpha_exactly(tmp_path):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes((80, 90, 100, 123))])
    client = DownloadClient([emoji_bytes((80, 90, 100, 124))])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False


@pytest.mark.parametrize("payload", [b"", b"not an image", emoji_bytes(size=(512, 512)),
                                     emoji_bytes(format="PNG")])
def test_verify_content_rejects_invalid_remote_images(tmp_path, payload):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes()])
    client = DownloadClient([payload])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False


def test_verify_content_rejects_animation(tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (100, 100), "red").save(
        buffer, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (100, 100), "blue")], duration=100, lossless=True)
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes()])
    assert run(TelegramTransport(DownloadClient([buffer.getvalue()])).verify_set_content(remote, pack)) is False


def test_verify_content_stops_oversized_download_before_buffer_exceeds_limit(tmp_path):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes()])
    client = DownloadClient([b"x" * (512 * 1024 + 1)])
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False
    assert client.maximum_buffer_size == 512 * 1024


@pytest.mark.parametrize("error", [ConnectionError("temporary failure"),
                                   errors.FloodWaitError(request=None, capture=30),
                                   errors.FileReferenceExpiredError(request=None)])
def test_verify_content_propagates_read_errors_without_retries(tmp_path, error):
    remote, pack = content_verification_pack(tmp_path, [emoji_bytes()])
    client = DownloadClient([error])
    with pytest.raises(type(error)):
        run(TelegramTransport(client).verify_set_content(remote, pack))
    assert len(client.downloads) == 1 and not client.calls and not client.uploads


@pytest.mark.parametrize("change", ["count", "duplicates", "bad_reference", "changed_local", "missing_local"])
def test_verify_content_checks_local_identity_before_download(tmp_path, change):
    contents = [emoji_bytes((220, 30, 50, 255)), emoji_bytes((20, 190, 230, 255))]
    remote, pack = content_verification_pack(tmp_path, contents)
    if change == "count":
        remote["documents"].pop()
    elif change == "duplicates":
        remote["documents"][1]["id"] = remote["documents"][0]["id"]
    elif change == "bad_reference":
        remote["documents"][0]["file_reference"] = "bad hex"
    elif change == "changed_local":
        (tmp_path / "0.webp").write_bytes(contents[1])
    else:
        (tmp_path / "0.webp").unlink()
    client = DownloadClient(contents)
    assert run(TelegramTransport(client).verify_set_content(remote, pack)) is False
    assert not client.downloads


def test_me_returns_only_account_identity_fields():
    client = FakeClient()
    client.user.phone = "should-not-leave-adapter"
    assert run(TelegramTransport(client).me()) == {"id": 99, "username": "owner", "premium": True}


def test_me_rejects_bot_account():
    client = FakeClient(me=SimpleNamespace(bot=True))
    with pytest.raises(TelegramResultError, match="personal"):
        run(TelegramTransport(client).me())


def test_owned_channel_matches_creator_title_broadcast_and_full_description():
    client = FakeClient([
        SimpleNamespace(full_chat=SimpleNamespace(about="another project")),
        SimpleNamespace(full_chat=SimpleNamespace(about="exact description")),
    ], dialogs=[channel(creator=False), channel(broadcast=False), channel(title="different"),
                channel(id=222), channel()])
    assert run(TelegramTransport(client).owned_channels("SLEEVE", "exact description")) == [CHANNEL]
    assert len(client.calls) == 2
    assert all(isinstance(call, functions.channels.GetFullChannelRequest) for call in client.calls)


def test_verify_channel_reads_saved_id_and_checks_current_identity():
    config={"title":"SLEEVE", "about":"Expected about", "username":"sleeve_test", "avatar_sha256":"a"*64}
    response=SimpleNamespace(full_chat=SimpleNamespace(id=123, about="Expected about"),
                             chats=[channel(id=999), channel()])
    client=FakeClient([response])
    assert run(TelegramTransport(client).verify_channel(CHANNEL,config)) is True
    request,=client.calls
    assert isinstance(request,functions.channels.GetFullChannelRequest)
    assert request.channel.channel_id==123 and request.channel.access_hash==456
    assert not client.uploads


@pytest.mark.parametrize("changes", [
    {"creator":False}, {"broadcast":False}, {"title":"Different channel"},
    {"username":"different_channel"}, {"username":None}, {"id":999},
])
def test_verify_channel_stops_on_identity_or_ownership_change(changes):
    config={"title":"SLEEVE", "about":"Expected about", "username":"sleeve_test"}
    client=FakeClient([SimpleNamespace(full_chat=SimpleNamespace(id=123, about="Expected about"),
                                      chats=[channel(**changes)])])
    assert run(TelegramTransport(client).verify_channel(CHANNEL,config)) is False
    assert len(client.calls)==1 and not client.uploads


@pytest.mark.parametrize("full_id,about,chats", [
    (123,"Changed description",[channel()]),
    (999,"Expected about",[channel()]),
    (123,"Expected about",[]),
    (123,"Expected about",[channel(),channel()]),
])
def test_verify_channel_rejects_wrong_full_result_or_ambiguity(full_id,about,chats):
    config={"title":"SLEEVE", "about":"Expected about", "username":"sleeve_test"}
    client=FakeClient([SimpleNamespace(full_chat=SimpleNamespace(id=full_id,about=about),chats=chats)])
    assert run(TelegramTransport(client).verify_channel(CHANNEL,config)) is False


@pytest.mark.parametrize("error", [errors.ChannelInvalidError(request=None),
                                   errors.ChannelPrivateError(request=None)])
def test_verify_channel_returns_false_when_channel_is_gone_or_inaccessible(error):
    client=FakeClient([error])
    assert run(TelegramTransport(client).verify_channel(CHANNEL,{})) is False


@pytest.mark.parametrize("error", [errors.FloodWaitError(request=None,capture=30),
                                   ConnectionError("network unavailable")])
def test_verify_channel_propagates_transient_failures(error):
    client=FakeClient([error])
    with pytest.raises(type(error)):
        run(TelegramTransport(client).verify_channel(CHANNEL,{}))
    assert len(client.calls)==1


def test_create_channel_returns_owned_broadcast_reference():
    client = FakeClient([SimpleNamespace(chats=[channel()])])
    assert run(TelegramTransport(client).create_channel("SLEEVE", "description")) == CHANNEL
    request, = client.calls
    assert request.broadcast is True and request.megagroup is None
    assert request.title == "SLEEVE" and request.about == "description"


def test_ambiguous_channel_creation_never_repeats():
    client = FakeClient([SimpleNamespace(chats=[channel(), channel(id=456)])])
    with pytest.raises(TelegramResultError, match="ambiguous"):
        run(TelegramTransport(client).create_channel("SLEEVE", "description"))
    assert len(client.calls) == 1


def test_username_collision_does_not_upload_avatar():
    client = FakeClient([errors.UsernameOccupiedError(request=None)])
    with pytest.raises(errors.UsernameOccupiedError):
        run(TelegramTransport(client).configure_channel(CHANNEL, "occupied", "avatar.png"))
    assert not client.uploads and len(client.calls) == 1
    assert CHANNEL["username"] == "sleeve_test"


@pytest.mark.parametrize("error", [None, errors.UsernameNotModifiedError(request=None),
                                  errors.ChatNotModifiedError(request=None)])
def test_configure_resumes_username_then_avatar(error):
    client = FakeClient([error or True, SimpleNamespace()])
    result = run(TelegramTransport(client).configure_channel(CHANNEL, "sleeve_new", "avatar.png"))
    assert result["username"] == "sleeve_new" and CHANNEL["username"] == "sleeve_test"
    assert isinstance(client.calls[0], functions.channels.UpdateUsernameRequest)
    assert isinstance(client.calls[1], functions.channels.EditPhotoRequest)
    assert isinstance(client.calls[1].photo, types.InputChatUploadedPhoto)
    assert client.uploads == ["avatar.png"]


def test_upload_static_emoji_as_document_without_sending_message(tmp_path):
    path = tmp_path / "cover.webp"
    Image.new("RGB", (100, 100), "red").save(path, format="WEBP", lossless=True)
    client = FakeClient([types.MessageMediaDocument(document=document(custom=False))])
    assert run(TelegramTransport(client).upload_emoji(path)) == DOCUMENT
    request, = client.calls
    assert isinstance(request, functions.messages.UploadMediaRequest)
    assert isinstance(request.peer, types.InputPeerSelf)
    assert request.media.mime_type == "image/webp" and request.media.force_file is True
    size = next(a for a in request.media.attributes if isinstance(a, types.DocumentAttributeImageSize))
    assert (size.w, size.h) == (100, 100)


@pytest.mark.parametrize("size,format", [((99, 100), "WEBP"), ((100, 100), "PNG")])
def test_upload_rejects_invalid_emoji_before_network(tmp_path, size, format):
    path = tmp_path / "cover.webp"
    Image.new("RGB", size).save(path, format=format)
    client = FakeClient()
    with pytest.raises(ValueError, match="100×100 WebP"):
        run(TelegramTransport(client).upload_emoji(path))
    assert not client.uploads and not client.calls


def test_upload_rejects_oversized_file_before_network(tmp_path):
    path = tmp_path / "cover.webp"
    path.write_bytes(b"x" * (512 * 1024 + 1))
    client = FakeClient()
    with pytest.raises(ValueError, match="512 KiB"):
        run(TelegramTransport(client).upload_emoji(path))
    assert not client.uploads


def test_get_set_serializes_actual_emoji_alt_and_refreshes_references():
    client = FakeClient([sticker_set()])
    result = run(TelegramTransport(client).get_set("sleeve_001"))
    assert result == {"id": 70, "access_hash": 80, "title": "SLEEVE / 001", "short_name": "sleeve_001",
                      "creator": True, "emojis": True, "documents": [DOCUMENT | {"alt": "💿", "keywords": []}]}
    assert client.calls[0].hash == 0


def test_missing_set_is_none_but_flood_error_is_not_hidden():
    client = FakeClient([errors.StickersetInvalidError(request=None), errors.FloodWaitError(request=None, capture=60)])
    adapter = TelegramTransport(client)
    assert run(adapter.get_set("missing")) is None
    with pytest.raises(errors.FloodWaitError):
        run(adapter.get_set("not-known"))
    assert len(client.calls) == 2


def test_create_set_uses_personal_owner_custom_emoji_flag_and_original_references():
    client = FakeClient([sticker_set()])
    result = run(TelegramTransport(client).create_set("SLEEVE / 001", "sleeve_001", [DOCUMENT]))
    request, = client.calls
    assert isinstance(request.user_id, types.InputUserSelf)
    assert request.emojis is True and request.masks is None and request.text_color is None
    assert request.stickers[0].document.file_reference == b"\x09\x0a"
    assert request.stickers[0].emoji == "💿"
    assert result["documents"][0]["alt"] == "💿"


@pytest.mark.parametrize("count", [0, 201])
def test_create_set_rejects_pack_size_before_request(count):
    client = FakeClient()
    with pytest.raises(ValueError, match="1–200"):
        run(TelegramTransport(client).create_set("title", "name", [DOCUMENT] * count))
    assert not client.calls


def test_custom_set_thumbnail_is_member_document_id():
    client = FakeClient([sticker_set()])
    run(TelegramTransport(client).set_thumbnail("sleeve_001", 7))
    request, = client.calls
    assert isinstance(request, functions.stickers.SetStickerSetThumbRequest)
    assert request.thumb is None and request.thumb_document_id == 7


def test_send_post_preserves_text_and_persistent_random_id_uses_matching_mapping():
    client = FakeClient([SimpleNamespace(updates=[types.UpdateMessageID(id=3, random_id=888),
                                                 types.UpdateMessageID(id=42, random_id=1234)])])
    result = run(TelegramTransport(client).send_post(CHANNEL, "**literal** / album", 1234))
    assert result == {"id": 42, "link": "https://t.me/sleeve_test/42"}
    request, = client.calls
    assert request.message == "**literal** / album" and request.random_id == 1234
    assert request.entities is None and request.no_webpage is True
    assert request.allow_paid_stars is None and request.allow_paid_floodskip is None


def test_emoji_grid_and_source_hyperlinks_use_utf16_units():
    text = "💿 💿\nArtist / Title"
    client = FakeClient([SimpleNamespace(updates=[types.UpdateMessageID(id=42, random_id=1)])])
    run(TelegramTransport(client).send_post(CHANNEL, text, 1,
        emoji_documents=[DOCUMENT | {"alt": "💿"}, DOCUMENT | {"id": 9, "alt": "💿"}],
        links=[{"offset": 6, "length": 14, "url": "https://artist.bandcamp.com/album/title"}]))
    entities = client.calls[0].entities
    assert [(e.offset, e.length) for e in entities] == [(0, 2), (3, 2), (6, 14)]
    assert [e.document_id for e in entities[:2]] == [7, 9]
    assert isinstance(entities[2], types.MessageEntityTextUrl)


@pytest.mark.parametrize("documents,links", [
    ([DOCUMENT | {"alt": "💿"}], None),
    ([DOCUMENT], None),
    (None, [{"offset": 0, "length": 1, "url": "https://example.org"}]),
    (None, [{"offset": 0, "length": 2, "url": "http://example.org"}]),
    (None, [{"offset": 0, "length": 99, "url": "https://example.org"}]),
])
def test_invalid_entity_stops_before_network(documents, links):
    client = FakeClient()
    with pytest.raises(ValueError):
        run(TelegramTransport(client).send_post(CHANNEL, "😀 hello", 1, documents, links))
    assert not client.calls


def test_new_channel_message_fallback_checks_peer_and_body():
    client = FakeClient([SimpleNamespace(updates=[
        types.UpdateNewChannelMessage(message("other"), pts=1, pts_count=1),
        types.UpdateNewChannelMessage(message(), pts=2, pts_count=1),
    ])])
    result = run(TelegramTransport(client).send_post(CHANNEL | {"username": None}, "post", 1))
    assert result == {"id": 42, "link": "https://t.me/c/123/42"}


def test_duplicate_random_id_reconciles_exact_channel_post_without_resending():
    client = FakeClient([errors.RandomIdDuplicateError(request=None)], messages=[
        message("post suffix"), message(post=False), message(peer_id=types.PeerChannel(999)), message()])
    assert run(TelegramTransport(client).send_post(CHANNEL, "post", 1234))["id"] == 42
    assert len(client.calls) == 1
    assert client.iter_message_calls[0][1] == 100


def test_duplicate_inflight_without_history_keeps_original_exception():
    client = FakeClient([errors.RandomIdDuplicateError(request=None)])
    with pytest.raises(errors.RandomIdDuplicateError):
        run(TelegramTransport(client).send_post(CHANNEL, "post", 1234))
    assert len(client.calls) == 1


def test_unknown_send_result_reconciles_then_raises_without_retry():
    client = FakeClient([SimpleNamespace(updates=[])])
    with pytest.raises(TelegramResultError, match="keep its random_id"):
        run(TelegramTransport(client).send_post(CHANNEL, "post", 1234))
    assert len(client.calls) == 1


@pytest.mark.parametrize("random_id", [0, True, 2**63, -(2**63) - 1])
def test_invalid_random_id_never_sends(random_id):
    client = FakeClient()
    with pytest.raises(ValueError, match="random_id"):
        run(TelegramTransport(client).send_post(CHANNEL, "post", random_id))
    assert not client.calls


def test_post_exists_requires_matching_channel_message():
    client = FakeClient()
    adapter = TelegramTransport(client)
    for candidate, expected in [(None, False), (message(), True), (message(post=False), False),
                                (message(peer_id=types.PeerChannel(999)), False)]:
        client.message_by_id = candidate
        assert run(adapter.post_exists(CHANNEL, 42)) is expected


def photo_file(tmp_path, *, format="PNG", size=(640, 640)):
    path = tmp_path / ("preview.jpg" if format == "JPEG" else "preview.png")
    Image.new("RGB", size, (90, 80, 70)).save(path, format=format)
    return path


def photo_media():
    return types.MessageMediaPhoto(photo=types.Photo(
        id=91, access_hash=92, file_reference=b"ref", date=NOW, sizes=[], dc_id=2))


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_send_photo_preserves_random_id_caption_and_entities(tmp_path, format):
    path = photo_file(tmp_path, format=format)
    text = "💿 Album #vinyl"
    links = [{"offset": 3, "length": 5, "url": "https://artist.bandcamp.com/album/title"}]
    client = FakeClient([SimpleNamespace(updates=[types.UpdateMessageID(id=42, random_id=1234)])])
    result = run(TelegramTransport(client).send_post(
        CHANNEL, text, 1234, [DOCUMENT | {"alt": "💿"}], links, path))
    request, = client.calls
    assert isinstance(request, functions.messages.SendMediaRequest)
    assert isinstance(request.media, types.InputMediaUploadedPhoto)
    assert request.peer.channel_id == CHANNEL["id"]
    assert request.message == text and request.random_id == 1234
    assert [(e.offset, e.length) for e in request.entities] == [(0, 2), (3, 5)]
    assert request.entities[0].document_id == DOCUMENT["id"]
    assert request.entities[1].url == links[0]["url"]
    assert request.allow_paid_stars is None and request.allow_paid_floodskip is None
    assert client.uploads == [str(path)]
    assert result == {"id": 42, "link": "https://t.me/sleeve_test/42"}


@pytest.mark.parametrize("operation", ["send_post", "edit_post"])
@pytest.mark.parametrize("invalid", ["missing", "not_image", "format", "size", "dimensions", "ratio", "caption", "animated"])
def test_photo_validation_stops_before_upload(tmp_path, operation, invalid):
    path = tmp_path / "preview.png"
    text = "caption"
    if invalid == "not_image":
        path.write_bytes(b"not an image")
    elif invalid == "format":
        path = photo_file(tmp_path, format="WEBP")
    elif invalid == "size":
        with path.open("wb") as handle:
            handle.truncate(10 * 1024 * 1024 + 1)
    elif invalid == "dimensions":
        path = photo_file(tmp_path, format="JPEG", size=(6000, 4001))
    elif invalid == "ratio":
        path = photo_file(tmp_path, size=(2100, 100))
    elif invalid == "caption":
        path = photo_file(tmp_path)
        text = "💿" * 513
    elif invalid == "animated":
        Image.new("RGB", (100, 100), "red").save(path, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (100, 100), "blue")], duration=100)
    client = FakeClient()
    method = getattr(TelegramTransport(client), operation)
    with pytest.raises(ValueError):
        if operation == "send_post":
            run(method(CHANNEL, text, 1234, photo_path=path))
        else:
            run(method(CHANNEL, 42, text, photo_path=path))
    assert not client.uploads and not client.calls


def test_photo_accepts_utf16_caption_and_aspect_ratio_boundaries(tmp_path):
    path = photo_file(tmp_path, size=(2000, 100))
    client = FakeClient([types.UpdateShortSentMessage(id=42, pts=1, pts_count=1, date=NOW)])
    assert run(TelegramTransport(client).send_post(CHANNEL, "💿" * 512, 1234, photo_path=path))["id"] == 42


@pytest.mark.parametrize("response", [errors.RandomIdDuplicateError(request=None), SimpleNamespace(updates=[])])
def test_photo_lost_mapping_reconciles_existing_caption_without_second_send(tmp_path, response):
    path = photo_file(tmp_path)
    client = FakeClient([response], messages=[message("caption", media=photo_media())])
    assert run(TelegramTransport(client).send_post(CHANNEL, "caption", 1234, photo_path=path))["id"] == 42
    assert len(client.calls) == len(client.uploads) == 1


def test_photo_unknown_result_without_matching_post_stops(tmp_path):
    path = photo_file(tmp_path)
    client = FakeClient([SimpleNamespace(updates=[])])
    with pytest.raises(TelegramResultError, match="random_id"):
        run(TelegramTransport(client).send_post(CHANNEL, "caption", 1234, photo_path=path))
    assert len(client.calls) == len(client.uploads) == 1


@pytest.mark.parametrize("operation", ["send_post", "edit_post"])
@pytest.mark.parametrize("error", [errors.FloodWaitError(request=None, capture=30), ConnectionError("lost reply")])
def test_photo_network_errors_propagate_without_retry(tmp_path, operation, error):
    path = photo_file(tmp_path)
    client = FakeClient([error])
    method = getattr(TelegramTransport(client), operation)
    with pytest.raises(type(error)):
        if operation == "send_post":
            run(method(CHANNEL, "caption", 1234, photo_path=path))
        else:
            run(method(CHANNEL, 42, "caption", photo_path=path))
    assert len(client.calls) == len(client.uploads) == 1


@pytest.mark.parametrize("with_photo", [False, True])
@pytest.mark.parametrize("response", [SimpleNamespace(updates=[]), errors.MessageNotModifiedError(request=None)])
def test_edit_post_updates_existing_message_and_verifies_content(tmp_path, with_photo, response):
    path = photo_file(tmp_path) if with_photo else None
    text = "💿 Album #vinyl"
    links = [{"offset": 3, "length": 5, "url": "https://artist.bandcamp.com/album/title"}]
    client = FakeClient([response])
    client.message_by_id = message(text, media=photo_media() if with_photo else None, entities=[
        types.MessageEntityCustomEmoji(offset=0, length=2, document_id=7),
        types.MessageEntityTextUrl(offset=3, length=5, url=links[0]["url"]),
        types.MessageEntityHashtag(offset=9, length=6),
    ])
    result = run(TelegramTransport(client).edit_post(
        CHANNEL, 42, text, [DOCUMENT | {"alt": "💿"}], links, path))
    assert result == {"id": 42, "link": "https://t.me/sleeve_test/42"}
    request, = client.calls
    assert isinstance(request, functions.messages.EditMessageRequest)
    assert request.id == 42 and request.message == text
    assert [(e.offset, e.length) for e in request.entities] == [(0, 2), (3, 5)]
    assert request.no_webpage == (None if with_photo else True)
    assert isinstance(request.media, types.InputMediaUploadedPhoto) if with_photo else request.media is None
    assert client.uploads == ([str(path)] if with_photo else [])


@pytest.mark.parametrize("actual", [None, message("different"), message(peer_id=types.PeerChannel(999)),
                                    message(id=99), message(post=False), message()])
def test_edit_photo_requires_matching_peer_body_id_and_photo(tmp_path, actual):
    path = photo_file(tmp_path)
    client = FakeClient([SimpleNamespace(updates=[])])
    client.message_by_id = actual
    with pytest.raises(TelegramResultError, match="could not be verified"):
        run(TelegramTransport(client).edit_post(CHANNEL, 42, "post", photo_path=path))
    assert len(client.calls) == 1


def test_edit_post_rejects_missing_requested_entities():
    client = FakeClient([SimpleNamespace(updates=[])])
    client.message_by_id = message("💿 Album")
    with pytest.raises(TelegramResultError):
        run(TelegramTransport(client).edit_post(CHANNEL, 42, "💿 Album", [DOCUMENT | {"alt": "💿"}]))


@pytest.mark.parametrize("message_id", [0, -1, True])
def test_edit_post_rejects_invalid_id_before_upload(tmp_path, message_id):
    client = FakeClient()
    with pytest.raises(ValueError, match="message ID"):
        run(TelegramTransport(client).edit_post(CHANNEL, message_id, "caption", photo_path=photo_file(tmp_path)))
    assert not client.calls and not client.uploads


def app_config(**values):
    return types.help.AppConfig(hash=12, config=types.JsonObject(value=[
        types.JsonObjectValue(key=key, value=types.JsonNumber(value=value)) for key, value in values.items()]))


@pytest.mark.parametrize("premium,expected", [(True, 4096), (False, 1024)])
def test_capabilities_reads_account_specific_current_caption_limit(premium, expected):
    client = FakeClient([app_config(caption_length_limit_default=1024, caption_length_limit_premium=4096)],
                        me=SimpleNamespace(id=99, username="owner", premium=premium, bot=False))
    transport = TelegramTransport(client)
    assert run(transport.capabilities()) == {"max_caption_length": expected}
    request, = client.calls
    assert isinstance(request, functions.help.GetAppConfigRequest) and request.hash == 0


@pytest.mark.parametrize("value", [0, -1, 4096.5, float("nan"), float("inf"), 1000001, True])
def test_capabilities_does_not_trust_invalid_remote_limits(value):
    transport = TelegramTransport(FakeClient([app_config(caption_length_limit_premium=value)]))
    assert run(transport.capabilities()) == {"max_caption_length": 1024}


def test_capabilities_missing_key_falls_back_to_default():
    transport = TelegramTransport(FakeClient([app_config(other_key=4096)]))
    assert run(transport.capabilities()) == {"max_caption_length": 1024}


def test_capabilities_does_not_keep_stale_premium_limit_after_downgrade(tmp_path):
    config = app_config(caption_length_limit_premium=4096, caption_length_limit_default=1024)
    client = FakeClient([config, config])
    transport = TelegramTransport(client)
    assert run(transport.capabilities())["max_caption_length"] == 4096
    client.user.premium = False
    assert run(transport.capabilities())["max_caption_length"] == 1024
    with pytest.raises(ValueError, match="1024"):
        run(transport.send_post(CHANNEL, "x" * 1025, 1234, photo_path=photo_file(tmp_path)))
    assert not client.uploads


@pytest.mark.parametrize("operation", ["send_post", "edit_post"])
def test_verified_premium_limit_allows_full_photo_caption(tmp_path, operation):
    text = "Альбом💿" * 256  # 2048 UTF-16 units, beyond the default limit.
    client = FakeClient([app_config(caption_length_limit_premium=4096),
                         SimpleNamespace(updates=[types.UpdateMessageID(id=42, random_id=1234)])])
    client.message_by_id = message(text, media=photo_media())
    transport = TelegramTransport(client)
    run(transport.capabilities())
    path = photo_file(tmp_path)
    result = (run(transport.send_post(CHANNEL, text, 1234, photo_path=path)) if operation == "send_post"
              else run(transport.edit_post(CHANNEL, 42, text, photo_path=path)))
    assert result["id"] == 42 and len(client.uploads) == 1
    with pytest.raises(ValueError, match="4096"):
        run(transport.send_post(CHANNEL, "💿" * 2049, 1234, photo_path=path))
    assert len(client.uploads) == 1


@pytest.mark.parametrize("response", [types.help.AppConfigNotModified(),
                                     types.help.AppConfig(hash=1, config=types.JsonNull())])
def test_capabilities_rejects_incomplete_config(response):
    transport = TelegramTransport(FakeClient([response]))
    with pytest.raises(TelegramResultError, match="configuration"):
        run(transport.capabilities())


def test_capability_network_failure_propagates_and_does_not_grant_large_caption(tmp_path):
    client = FakeClient([app_config(caption_length_limit_premium=4096),
                         errors.FloodWaitError(request=None, capture=30)])
    transport = TelegramTransport(client)
    run(transport.capabilities())
    with pytest.raises(errors.FloodWaitError):
        run(transport.capabilities())
    with pytest.raises(ValueError, match="1024"):
        run(transport.send_post(CHANNEL, "x" * 1025, 1234, photo_path=photo_file(tmp_path)))
    assert not client.uploads


def keyword_set(words, **changes):
    result = sticker_set(**changes)
    result.keywords = [types.StickerKeyword(document_id=7, keyword=words)]
    return result


def test_create_set_accepts_per_document_search_keywords():
    client = FakeClient([keyword_set(["Album title", "Artist"])])
    remote = run(TelegramTransport(client).create_set(
        "SLEEVE", "sleeve_001", [DOCUMENT], keywords=["Album title, Artist"]))
    request, = client.calls
    assert request.stickers[0].keywords == "Album title,Artist"
    assert request.stickers[0].emoji == "💿"
    assert remote["documents"][0]["keywords"] == ["Album title", "Artist"]


def test_get_set_maps_keywords_by_document_id_not_returned_order():
    result = keyword_set(["Original"])
    result.documents.append(document(id=9))
    result.keywords.insert(0, types.StickerKeyword(document_id=9, keyword=["Second", "Другой"]))
    client = FakeClient([result])
    remote = run(TelegramTransport(client).get_set("sleeve_001"))
    assert [item["keywords"] for item in remote["documents"]] == [["Original"], ["Second", "Другой"]]


@pytest.mark.parametrize("value,returned", [("Album title,Artist", ["artist", "album title"]), ("", [])])
def test_change_keywords_preserves_art_and_emoji_and_verifies_reply(value, returned):
    client = FakeClient([keyword_set(returned)])
    remote = run(TelegramTransport(client).change_keywords(DOCUMENT, value))
    request, = client.calls
    assert isinstance(request, functions.stickers.ChangeStickerRequest)
    assert request.sticker.id == 7 and request.sticker.file_reference == b"\x09\x0a"
    assert request.emoji is None and request.mask_coords is None
    assert request.keywords == value
    assert int.from_bytes(bytes(request)[4:8], "little") & 4  # Empty string also explicitly clears keywords.
    assert remote["documents"][0]["keywords"] == returned


@pytest.mark.parametrize("value", ["x" * 65, "💿" * 33, ",".join(str(i) for i in range(21)),
                                   "Album,,Artist", "Album,album", "Album\nArtist", None])
def test_invalid_search_keywords_stop_before_any_request(value):
    client = FakeClient()
    transport = TelegramTransport(client)
    with pytest.raises(ValueError):
        run(transport.create_set("SLEEVE", "sleeve_001", [DOCUMENT], keywords=[value]))
    with pytest.raises(ValueError):
        run(transport.change_keywords(DOCUMENT, value))
    assert not client.calls


@pytest.mark.parametrize("keywords", [[], ["one", "two"], "text"])
def test_create_keywords_count_must_match_documents(keywords):
    client = FakeClient()
    with pytest.raises(ValueError, match="count"):
        run(TelegramTransport(client).create_set("SLEEVE", "sleeve_001", [DOCUMENT], keywords=keywords))
    assert not client.calls


def test_search_keywords_total_limit_accepts_exact_boundary():
    words = ["а" * 32, "б" * 32]
    client = FakeClient([keyword_set(words)])
    assert run(TelegramTransport(client).change_keywords(DOCUMENT, ",".join(words)))["creator"] is True


@pytest.mark.parametrize("result", [keyword_set(["Other"]), keyword_set(["Album"], creator=False),
                                    keyword_set(["Album"], emojis=False), sticker_set()])
def test_change_keywords_rejects_unverified_result(result):
    client = FakeClient([result])
    with pytest.raises(TelegramResultError, match="keywords"):
        run(TelegramTransport(client).change_keywords(DOCUMENT, "Album"))
    assert len(client.calls) == 1


def test_keyword_change_flood_wait_propagates_without_retry():
    client = FakeClient([errors.FloodWaitError(request=None, capture=30)])
    with pytest.raises(errors.FloodWaitError):
        run(TelegramTransport(client).change_keywords(DOCUMENT, "Album"))
    assert len(client.calls) == 1


@pytest.mark.parametrize("with_photo", [False, True])
def test_send_native_formatting_preserves_utf16_and_other_entities(tmp_path, with_photo):
    text = "💿 Album ♡"
    client = FakeClient([SimpleNamespace(updates=[types.UpdateMessageID(id=42, random_id=1234)])])
    run(TelegramTransport(client).send_post(
        CHANNEL, text, 1234, [DOCUMENT | {"alt": "💿"}],
        [{"offset": 3, "length": 5, "url": "https://example.org/album"}],
        photo_file(tmp_path) if with_photo else None,
        formatting=[{"type": "bold", "offset": 0, "length": 8},
                    {"type": "italic", "offset": 3, "length": 5}]))
    entities = client.calls[0].entities
    assert [type(entity) for entity in entities] == [
        types.MessageEntityBold, types.MessageEntityCustomEmoji,
        types.MessageEntityTextUrl, types.MessageEntityItalic]
    assert [(entity.offset, entity.length) for entity in entities] == [(0, 8), (0, 2), (3, 5), (3, 5)]
    assert client.calls[0].message == text


@pytest.mark.parametrize("span", [
    {"type": "underline", "offset": 0, "length": 2},
    {"type": ["bold"], "offset": 0, "length": 2},
    {"type": "bold", "offset": 0, "length": 1},
    {"type": "italic", "offset": 1, "length": 1},
    {"type": "bold", "offset": -1, "length": 2},
    {"type": "bold", "offset": 0, "length": 100},
    {"type": "italic", "offset": 0, "length": 0},
    {"type": "bold", "offset": True, "length": 2},
    {"type": "bold", "offset": 0},
    "bold",
])
def test_invalid_formatting_is_rejected_before_upload_or_request(tmp_path, span):
    client = FakeClient()
    transport = TelegramTransport(client)
    with pytest.raises(ValueError):
        run(transport.send_post(CHANNEL, "💿 Album", 1234, photo_path=photo_file(tmp_path), formatting=[span]))
    with pytest.raises(ValueError):
        run(transport.edit_post(CHANNEL, 42, "💿 Album", formatting=[span]))
    assert not client.calls and not client.uploads


@pytest.mark.parametrize("actual_entities,verified", [
    ([types.MessageEntityBold(offset=0, length=5), types.MessageEntityItalic(offset=6, length=5)], True),
    ([types.MessageEntityBold(offset=0, length=5)], False),
    ([types.MessageEntityBold(offset=0, length=4), types.MessageEntityItalic(offset=6, length=5)], False),
    ([types.MessageEntityItalic(offset=0, length=5), types.MessageEntityItalic(offset=6, length=5)], False),
])
def test_edit_post_verifies_native_formatting_on_readback(actual_entities, verified):
    client = FakeClient([SimpleNamespace(updates=[])])
    client.message_by_id = message("Album title", entities=actual_entities)
    operation = TelegramTransport(client).edit_post(CHANNEL, 42, "Album title", formatting=[
        {"type": "bold", "offset": 0, "length": 5}, {"type": "italic", "offset": 6, "length": 5}])
    if verified:
        assert run(operation)["id"] == 42
    else:
        with pytest.raises(TelegramResultError, match="verified"):
            run(operation)


@pytest.mark.parametrize("style", ["bold", "italic"])
@pytest.mark.parametrize("ranges,verified", [
    ([(0, 6), (6, 7)], True),  # Server split around the mention.
    ([(6, 7), (0, 8)], True),  # Same coverage, overlap and different order.
    ([(0, 13), (0, 13)], True),  # Redundant style covers the same characters.
    ([(0, 5), (6, 7)], False),  # The intervening character lost its style.
    ([(0, 6)], False),  # Mention lost its style entirely.
])
def test_edit_verifies_equivalent_style_coverage_around_mentions(style, ranges, verified):
    constructor = types.MessageEntityBold if style == "bold" else types.MessageEntityItalic
    client = FakeClient([errors.MessageNotModifiedError(request=None)])
    client.message_by_id = message("Hello @sleeve", entities=[
        constructor(offset=offset, length=length) for offset, length in ranges
    ] + [types.MessageEntityMention(offset=6, length=7)])
    operation = TelegramTransport(client).edit_post(CHANNEL, 42, "Hello @sleeve", formatting=[
        {"type": style, "offset": 0, "length": 13}])
    if verified:
        assert run(operation)["id"] == 42
    else:
        with pytest.raises(TelegramResultError, match="verified"):
            run(operation)


def test_style_normalization_does_not_merge_links_or_custom_emojis():
    from coverpacks.telegram_transport import _explicit_entity_records
    actual = [
        types.MessageEntityTextUrl(offset=0, length=3, url="https://example.org"),
        types.MessageEntityTextUrl(offset=3, length=3, url="https://example.org"),
        types.MessageEntityCustomEmoji(offset=6, length=2, document_id=7),
        types.MessageEntityCustomEmoji(offset=8, length=2, document_id=7),
        types.MessageEntityBold(offset=0, length=3),
        types.MessageEntityItalic(offset=3, length=3),
    ]
    assert _explicit_entity_records(actual) == [
        ("bold", 0, 3), ("emoji", 6, 2, 7), ("emoji", 8, 2, 7), ("italic", 3, 3),
        ("link", 0, 3, "https://example.org"), ("link", 3, 3, "https://example.org"),
    ]

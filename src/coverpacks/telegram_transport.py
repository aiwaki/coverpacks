"""Small MTProto adapter. Persistence, authorization and retry policy live upstream.

The injected client must already be connected and authorized. This module never
reads credentials, starts login, sleeps, or retries a state-changing operation.
Only selected public metadata and document references leave the adapter.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import logging
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat, UnidentifiedImageError
from telethon import errors, functions, types


class TelegramResultError(RuntimeError):
    """The request may have succeeded but its result could not be reconciled."""


_STATIC_EMOJI_MAX_BYTES = 512 * 1024
_CONTENT_RMSE_LIMIT = 32
_CONTENT_LOWPASS_SIZE = (32, 32)
_CONTENT_LOWPASS_LIMIT = 12
_LOG = logging.getLogger(__name__)


def _content_mismatch(reason: str, **facts) -> bool:
    # Callers pass only fixed reason labels, positions and numeric image facts.
    # Telegram references, hashes, paths and source bytes must never be logged.
    _LOG.warning("%s", json.dumps({"event": "emoji_content_verification_failed", "reason": reason, **facts},
                                   sort_keys=True))
    return False


def _image_facts(content: bytes) -> dict:
    facts = {"bytes": len(content)}
    try:
        with Image.open(BytesIO(content)) as image:
            facts.update(width=image.width, height=image.height, format=image.format,
                         frames=getattr(image, "n_frames", 1))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        facts["decoded"] = False
    return facts


class _DownloadTooLarge(ValueError):
    pass


class _BoundedDownload(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > _STATIC_EMOJI_MAX_BYTES:
            raise _DownloadTooLarge("Downloaded custom emoji exceeds its size limit")
        return super().write(data)


def _static_emoji_rgba(content: bytes) -> Image.Image | None:
    if not content or len(content) > _STATIC_EMOJI_MAX_BYTES:
        return None
    try:
        with Image.open(BytesIO(content)) as image:
            if (image.format != "WEBP" or image.size != (100, 100)
                    or getattr(image, "n_frames", 1) != 1):
                return None
            return image.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def _rgb_rmse(actual: Image.Image, expected: Image.Image) -> float:
    difference = ImageChops.difference(actual, expected)
    return math.sqrt(sum(ImageStat.Stat(difference).sum2) / (actual.width * actual.height * 3))


def _visible_rgb(rgba: Image.Image) -> Image.Image:
    """Weight colors by alpha; transparent WebP pixels have arbitrary RGB."""
    return ImageChops.multiply(rgba.convert("RGB"), rgba.getchannel("A").convert("RGB"))


def _validate_photo(path: str | Path, caption: str, max_caption_length: int) -> Path:
    if len(caption.encode("utf-16-le")) // 2 > max_caption_length:
        raise ValueError(f"Photo caption must not exceed {max_caption_length} UTF-16 units")
    path = Path(path)
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError("Photo must be an existing file no larger than 10 MiB")
        with Image.open(path) as image:
            width, height = image.size
            if (image.format not in {"PNG", "JPEG"} or getattr(image, "n_frames", 1) != 1
                    or width + height > 10000 or max(width, height) > 20 * min(width, height)):
                raise ValueError("Expected a static PNG/JPEG photo with dimension sum ≤10000 and aspect ratio ≤20")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Photo could not be read as a valid PNG/JPEG image") from error
    return path


def _explicit_entity_records(entities: list | None) -> list[tuple]:
    result = []
    styles = {"bold": [], "italic": []}
    for entity in entities or []:
        if isinstance(entity, types.MessageEntityCustomEmoji):
            result.append(("emoji", entity.offset, entity.length, entity.document_id))
        elif isinstance(entity, types.MessageEntityTextUrl):
            result.append(("link", entity.offset, entity.length, entity.url))
        elif isinstance(entity, types.MessageEntityBold):
            styles["bold"].append((entity.offset, entity.offset + entity.length))
        elif isinstance(entity, types.MessageEntityItalic):
            styles["italic"].append((entity.offset, entity.offset + entity.length))
    # Telegram can partition one style around mentions or other entities.
    # Compare coverage, while preserving every gap and keeping style types apart.
    for kind, ranges in styles.items():
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result.extend((kind, start, end - start) for start, end in merged)
    return sorted(result)


def _channel_record(channel: Any) -> dict:
    if not isinstance(channel, types.Channel) or channel.access_hash is None:
        raise TelegramResultError("Telegram returned no usable channel reference")
    return {
        "id": channel.id,
        "access_hash": channel.access_hash,
        "title": channel.title,
        "username": channel.username,
    }


def _input_channel(channel: dict) -> types.InputChannel:
    return types.InputChannel(channel_id=int(channel["id"]), access_hash=int(channel["access_hash"]))


def _input_peer(channel: dict) -> types.InputPeerChannel:
    return types.InputPeerChannel(channel_id=int(channel["id"]), access_hash=int(channel["access_hash"]))


def _document_record(document: Any, *, include_alt: bool = False) -> dict:
    if not isinstance(document, types.Document):
        raise TelegramResultError("Telegram returned no usable document reference")
    result = {
        "id": document.id,
        "access_hash": document.access_hash,
        "file_reference": document.file_reference.hex(),
    }
    if include_alt:
        result["alt"] = next((attribute.alt for attribute in document.attributes
                              if isinstance(attribute, types.DocumentAttributeCustomEmoji)), None)
    return result


def _input_document(document: dict) -> types.InputDocument:
    return types.InputDocument(id=int(document["id"]), access_hash=int(document["access_hash"]),
                               file_reference=bytes.fromhex(document["file_reference"]))


def _set_record(result: Any) -> dict:
    if not isinstance(result, types.messages.StickerSet):
        raise TelegramResultError("Telegram returned no full sticker set")
    sticker_set = result.set
    keywords = {entry.document_id: list(entry.keyword) for entry in result.keywords}
    return {
        "id": sticker_set.id,
        "access_hash": sticker_set.access_hash,
        "short_name": sticker_set.short_name,
        "title": sticker_set.title,
        "creator": bool(sticker_set.creator),
        "emojis": bool(sticker_set.emojis),
        "documents": [_document_record(document, include_alt=True)
                      | {"keywords": keywords.get(document.id, [])} for document in result.documents],
    }


def _validated_keywords(value: str) -> tuple[str, list[str]]:
    if not isinstance(value, str):
        raise ValueError("Sticker keywords must be a comma-separated string")
    words = [word.strip() for word in value.split(",")] if value else []
    if (len(words) > 20 or any(not word or any(ord(char) < 32 for char in word) for word in words)
            or sum(len(word.encode("utf-16-le")) // 2 for word in words) > 64):
        raise ValueError("Sticker keywords require at most 20 non-empty phrases and 64 UTF-16 units in total")
    if len({word.casefold() for word in words}) != len(words):
        raise ValueError("Sticker keywords must not contain duplicate phrases")
    return ",".join(words), words


def _post_record(channel: dict, message_id: int) -> dict:
    if channel.get("username"):
        link = f"https://t.me/{channel['username']}/{message_id}"
    else:
        link = f"https://t.me/c/{channel['id']}/{message_id}"
    return {"id": message_id, "link": link}


def _matches_post(message: Any, channel: dict, text: str | None = None) -> bool:
    return (isinstance(message, types.Message)
            and bool(message.post)
            and isinstance(message.peer_id, types.PeerChannel)
            and message.peer_id.channel_id == int(channel["id"])
            and (text is None or message.message == text))


def _message_entities(text: str, documents: list[dict] | None,
                      links: list[dict] | None, formatting: list[dict] | None = None) -> list:
    entities = []
    cursor = 0
    for document in documents or []:
        alt = document.get("alt")
        if not isinstance(alt, str) or not alt:
            raise ValueError("Custom emoji document must include its actual alt text")
        position = text.find(alt, cursor)
        if position == -1:
            raise ValueError("Custom emoji alt text was not found in the post")
        entities.append(types.MessageEntityCustomEmoji(
            offset=len(text[:position].encode("utf-16-le")) // 2,
            length=len(alt.encode("utf-16-le")) // 2,
            document_id=int(document["id"])))
        cursor = position + len(alt)
    encoded = text.encode("utf-16-le")
    for link in links or []:
        offset, length = link["offset"], link["length"]
        if (type(offset) is not int or type(length) is not int or offset < 0
                or length < 1 or 2 * (offset + length) > len(encoded)):
            raise ValueError("Link entity has invalid UTF-16 bounds")
        try:
            encoded[2 * offset:2 * (offset + length)].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise ValueError("Link entity splits a Unicode character") from error
        url = link["url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Post source links must be HTTPS URLs without credentials")
        entities.append(types.MessageEntityTextUrl(offset=offset, length=length, url=url))
    for span in formatting or []:
        kind = span.get("type") if isinstance(span, dict) else None
        if not isinstance(kind, str) or kind not in {"bold", "italic"}:
            raise ValueError("Formatting entity type must be bold or italic")
        offset, length = span.get("offset"), span.get("length")
        if (type(offset) is not int or type(length) is not int or offset < 0
                or length < 1 or 2 * (offset + length) > len(encoded)):
            raise ValueError("Formatting entity has invalid UTF-16 bounds")
        try:
            encoded[2 * offset:2 * (offset + length)].decode("utf-16-le")
        except UnicodeDecodeError as error:
            raise ValueError("Formatting entity splits a Unicode character") from error
        constructor = types.MessageEntityBold if span["type"] == "bold" else types.MessageEntityItalic
        entities.append(constructor(offset=offset, length=length))
    return sorted(entities, key=lambda entity: (entity.offset, -entity.length))


class TelegramTransport:
    """Transport for an existing personal Telethon session, with no hidden retries."""

    def __init__(self, client: Any):
        self.client = client
        self._max_caption_length = 1024

    async def me(self) -> dict:
        user = await self.client.get_me()
        if not user or getattr(user, "bot", False):
            raise TelegramResultError("An authorized personal Telegram account is required")
        return {"id": user.id, "premium": bool(user.premium), "username": user.username}

    async def capabilities(self) -> dict:
        """Read account-specific limits; unverified/missing values stay conservative."""
        self._max_caption_length = 1024
        account = await self.me()
        result = await self.client(functions.help.GetAppConfigRequest(hash=0))
        if not isinstance(result, types.help.AppConfig) or not isinstance(result.config, types.JsonObject):
            raise TelegramResultError("Telegram returned no usable application configuration")
        key = "caption_length_limit_premium" if account["premium"] else "caption_length_limit_default"
        matches = [entry.value for entry in result.config.value if entry.key == key]
        if len(matches) == 1 and isinstance(matches[0], types.JsonNumber):
            value = matches[0].value
            if (type(value) in {int, float} and math.isfinite(value) and int(value) == value
                    and 1 <= value <= 1000000):
                self._max_caption_length = int(value)
        return {"max_caption_length": self._max_caption_length}

    async def owned_channels(self, title: str, about: str) -> list[dict]:
        matches = []
        async for dialog in self.client.iter_dialogs():
            channel = dialog.entity
            if (not isinstance(channel, types.Channel) or not channel.creator
                    or not channel.broadcast or channel.title != title):
                continue
            record = _channel_record(channel)
            full = await self.client(functions.channels.GetFullChannelRequest(channel=_input_channel(record)))
            if full.full_chat.about == about:
                matches.append(record)
        return matches

    async def verify_channel(self, channel: dict, config: dict) -> bool:
        """Read the saved peer again before publication; never resolve by username."""
        try:
            full = await self.client(functions.channels.GetFullChannelRequest(
                channel=_input_channel(channel)))
        except (errors.ChannelInvalidError, errors.ChannelPrivateError):
            return False
        expected_id = int(channel["id"])
        full_chat = getattr(full, "full_chat", None)
        if (getattr(full_chat, "id", None) != expected_id
                or getattr(full_chat, "about", None) != config["about"]):
            return False
        matches = [chat for chat in getattr(full, "chats", [])
                   if isinstance(chat, types.Channel) and chat.id == expected_id]
        if len(matches) != 1:
            return False
        actual = matches[0]
        return bool(actual.creator and actual.broadcast
                    and actual.title == config["title"]
                    and actual.username == config["username"])

    async def create_channel(self, title: str, about: str) -> dict:
        updates = await self.client(functions.channels.CreateChannelRequest(
            title=title, about=about, broadcast=True))
        matches = [chat for chat in getattr(updates, "chats", [])
                   if isinstance(chat, types.Channel) and chat.creator
                   and chat.broadcast and chat.title == title]
        if len(matches) != 1:
            raise TelegramResultError("Channel creation result is ambiguous; reconcile before retrying")
        return _channel_record(matches[0])

    async def configure_channel(self, channel: dict, username: str | None,
                                avatar_path: str | Path | None) -> dict:
        result = dict(channel)
        if username and result.get("username") != username:
            try:
                await self.client(functions.channels.UpdateUsernameRequest(
                    channel=_input_channel(channel), username=username))
            except (errors.UsernameNotModifiedError, errors.ChatNotModifiedError):
                pass  # A previous attempt may have applied it before an avatar failure.
            result["username"] = username
        if avatar_path is not None:
            file = await self.client.upload_file(str(avatar_path))
            await self.client(functions.channels.EditPhotoRequest(
                channel=_input_channel(channel), photo=types.InputChatUploadedPhoto(file=file)))
        return result

    async def upload_emoji(self, path: str | Path) -> dict:
        path = Path(path)
        if path.stat().st_size > 512 * 1024:
            raise ValueError("Static custom emoji must not exceed 512 KiB")
        with Image.open(path) as image:
            if image.format != "WEBP" or image.size != (100, 100) or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Expected one static 100×100 WebP custom emoji")
            image.verify()
        file = await self.client.upload_file(str(path))
        result = await self.client(functions.messages.UploadMediaRequest(
            peer=types.InputPeerSelf(),
            media=types.InputMediaUploadedDocument(
                file=file, mime_type="image/webp", force_file=True,
                attributes=[types.DocumentAttributeFilename(file_name=path.name),
                            types.DocumentAttributeImageSize(w=100, h=100)],
            ),
        ))
        return _document_record(getattr(result, "document", None))

    async def get_set(self, short_name: str) -> dict | None:
        try:
            result = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=short_name), hash=0))
        except errors.StickersetInvalidError:
            return None
        return _set_record(result)

    async def verify_set_content(self, remote: dict, pack: dict) -> bool:
        """Recognize server-converted images before adopting their new document IDs.

        Telegram converts static uploads to WebP and can change both IDs and
        pixels. A positional match must be close in visible color, preserve alpha
        exactly, and be clearly better than every other source. Ambiguity fails closed.
        Transport errors propagate so the caller can retry this read later.
        """
        items, documents = pack.get("items", []), remote.get("documents", [])
        if not 1 <= len(items) <= 200 or len(documents) != len(items):
            return _content_mismatch("item_count", expected_count=len(items), actual_count=len(documents))
        try:
            document_ids = [int(document["id"]) for document in documents]
            if len(set(document_ids)) != len(document_ids):
                return _content_mismatch("duplicate_documents", actual_count=len(documents))
            locations = [types.InputDocumentFileLocation(
                id=int(document["id"]), access_hash=int(document["access_hash"]),
                file_reference=bytes.fromhex(document["file_reference"]), thumb_size="")
                for document in documents]
        except (KeyError, TypeError, ValueError):
            return _content_mismatch("invalid_document_reference")
        sources = []
        try:
            for item in items:
                # Bound local reads too; the manifest has already been validated
                # upstream, but recheck its hash at the moment of comparison.
                with Path(item["webp_path"]).open("rb") as source:
                    content = source.read(_STATIC_EMOJI_MAX_BYTES + 1)
                if hashlib.sha256(content).hexdigest() != item["webp_sha256"]:
                    return _content_mismatch("local_checksum", position=len(sources) + 1, bytes=len(content))
                rgba = _static_emoji_rgba(content)
                if rgba is None:
                    return _content_mismatch("invalid_local_image", position=len(sources) + 1,
                                             expected_width=100, expected_height=100, actual=_image_facts(content))
                rgb = _visible_rgb(rgba)
                sources.append((rgb, rgba.getchannel("A"), rgb.resize(_CONTENT_LOWPASS_SIZE, Image.Resampling.LANCZOS)))
        except (KeyError, TypeError, OSError, ValueError):
            return _content_mismatch("local_image_unavailable", position=len(sources) + 1)
        for index, location in enumerate(locations):
            with _BoundedDownload() as buffer:
                try:
                    await self.client.download_file(location, file=buffer, part_size_kb=64)
                except _DownloadTooLarge:
                    return _content_mismatch("remote_image_too_large", position=index + 1,
                                             maximum_bytes=_STATIC_EMOJI_MAX_BYTES)
                content = buffer.getvalue()
                actual = _static_emoji_rgba(content)
            if actual is None:
                return _content_mismatch("invalid_remote_image", position=index + 1,
                                         expected_width=100, expected_height=100, actual=_image_facts(content))
            actual_alpha, expected_alpha = actual.getchannel("A"), sources[index][1]
            if actual_alpha.tobytes() != expected_alpha.tobytes():
                difference = ImageChops.difference(actual_alpha, expected_alpha)
                return _content_mismatch("alpha_mismatch", position=index + 1,
                    mismatch_pixels=sum(difference.histogram()[1:]), maximum_alpha_difference=difference.getextrema()[1],
                    expected_alpha_range=expected_alpha.getextrema(), actual_alpha_range=actual_alpha.getextrema())
            rgb = _visible_rgb(actual)
            own_distance = _rgb_rmse(rgb, sources[index][0])
            # Lossy WebP subsamples chroma: saturated fine text can lose edge
            # colors while preserving its layout. Both scales must match; the
            # smaller image imposes a tighter bound on broad color/layout drift.
            lowpass_distance = _rgb_rmse(rgb.resize(_CONTENT_LOWPASS_SIZE, Image.Resampling.LANCZOS), sources[index][2])
            others = [(_rgb_rmse(rgb, source[0]), other + 1)
                      for other, source in enumerate(sources) if other != index]
            nearest_distance, nearest_position = min(others) if others else (None, None)
            minimum_other_distance = max(2 * own_distance, own_distance + 8)
            facts = {"position": index + 1, "own_distance": round(own_distance, 4),
                     "nearest_other_distance": round(nearest_distance, 4) if nearest_distance is not None else None,
                     "nearest_other_position": nearest_position, "maximum_own_distance": _CONTENT_RMSE_LIMIT,
                     "lowpass_distance": round(lowpass_distance, 4), "maximum_lowpass_distance": _CONTENT_LOWPASS_LIMIT,
                     "minimum_other_distance": round(minimum_other_distance, 4)}
            if own_distance > _CONTENT_RMSE_LIMIT:
                return _content_mismatch("color_distance", **facts)
            if lowpass_distance > _CONTENT_LOWPASS_LIMIT:
                return _content_mismatch("lowpass_distance", **facts)
            if nearest_distance is not None and nearest_distance < minimum_other_distance:
                return _content_mismatch("ambiguous_source", **facts)
        return True

    async def create_set(self, title: str, short_name: str, documents: list[dict],
                         emoji: str = "💿", keywords: list[str] | None = None) -> dict:
        if not 1 <= len(documents) <= 200:
            raise ValueError("A custom emoji pack must contain 1–200 items")
        if keywords is not None and (not isinstance(keywords, list) or len(keywords) != len(documents)):
            raise ValueError("Sticker keywords must match the document count")
        encoded_keywords = ([_validated_keywords(value)[0] for value in keywords]
                            if keywords is not None else [None] * len(documents))
        result = await self.client(functions.stickers.CreateStickerSetRequest(
            user_id=types.InputUserSelf(), title=title, short_name=short_name,
            stickers=[types.InputStickerSetItem(document=_input_document(document), emoji=emoji,
                                               keywords=keyword)
                      for document, keyword in zip(documents, encoded_keywords)],
            emojis=True, software="coverpacks",
        ))
        return _set_record(result)

    async def change_keywords(self, document: dict, keywords: str) -> dict:
        """Update only the search phrases of an already verified pack member."""
        encoded, expected = _validated_keywords(keywords)
        result = await self.client(functions.stickers.ChangeStickerRequest(
            sticker=_input_document(document), keywords=encoded))
        remote = _set_record(result)
        matches = [item for item in remote["documents"] if item["id"] == int(document["id"])]
        if (not remote["creator"] or not remote["emojis"] or len(matches) != 1
                or sorted(word.casefold() for word in matches[0]["keywords"])
                != sorted(word.casefold() for word in expected)):
            raise TelegramResultError("Updated custom emoji keywords could not be verified")
        return remote

    async def set_thumbnail(self, short_name: str, document_id: int) -> None:
        await self.client(functions.stickers.SetStickerSetThumbRequest(
            stickerset=types.InputStickerSetShortName(short_name=short_name),
            thumb_document_id=document_id))

    async def find_post(self, channel: dict, text: str) -> dict | None:
        async for message in self.client.iter_messages(_input_peer(channel), limit=100):
            if _matches_post(message, channel, text):
                return _post_record(channel, message.id)
        return None

    async def send_post(self, channel: dict, text: str, random_id: int,
                        emoji_documents: list[dict] | None = None,
                        links: list[dict] | None = None,
                        photo_path: str | Path | None = None, *,
                        formatting: list[dict] | None = None) -> dict:
        if not isinstance(random_id, int) or isinstance(random_id, bool) or not -(2**63) <= random_id < 2**63 or random_id == 0:
            raise ValueError("A persistent non-zero signed 64-bit random_id is required")
        entities = _message_entities(text, emoji_documents, links, formatting)
        if photo_path is not None:
            photo = _validate_photo(photo_path, text, self._max_caption_length)
            file = await self.client.upload_file(str(photo))
            request = functions.messages.SendMediaRequest(
                peer=_input_peer(channel), media=types.InputMediaUploadedPhoto(file=file),
                message=text, random_id=random_id, entities=entities or None)
        else:
            request = functions.messages.SendMessageRequest(
                peer=_input_peer(channel), message=text, random_id=random_id, no_webpage=True,
                entities=entities or None)
        try:
            result = await self.client(request)
        except errors.RandomIdDuplicateError:
            found = await self.find_post(channel, text)
            if found:
                return found
            raise
        if isinstance(result, types.UpdateShortSentMessage):
            return _post_record(channel, result.id)
        updates = getattr(result, "updates", [])
        for update in updates:
            if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
                return _post_record(channel, update.id)
        for update in updates:
            if isinstance(update, types.UpdateNewChannelMessage) and _matches_post(update.message, channel, text):
                return _post_record(channel, update.message.id)
        found = await self.find_post(channel, text)
        if found:
            return found
        raise TelegramResultError("Post may have been sent; keep its random_id and reconcile before retrying")

    async def edit_post(self, channel: dict, message_id: int, text: str,
                        emoji_documents: list[dict] | None = None,
                        links: list[dict] | None = None,
                        photo_path: str | Path | None = None, *,
                        formatting: list[dict] | None = None) -> dict:
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("An existing positive message ID is required")
        entities = _message_entities(text, emoji_documents, links, formatting)
        media = None
        if photo_path is not None:
            photo = _validate_photo(photo_path, text, self._max_caption_length)
            file = await self.client.upload_file(str(photo))
            media = types.InputMediaUploadedPhoto(file=file)
        try:
            await self.client(functions.messages.EditMessageRequest(
                peer=_input_peer(channel), id=message_id, message=text,
                entities=entities, media=media, no_webpage=True if media is None else None))
        except errors.MessageNotModifiedError:
            pass
        actual = await self.client.get_messages(_input_peer(channel), ids=message_id)
        if (not _matches_post(actual, channel, text) or actual.id != message_id
                or _explicit_entity_records(actual.entities) != _explicit_entity_records(entities)
                or (photo_path is not None and (not isinstance(actual.media, types.MessageMediaPhoto)
                                                or not isinstance(actual.media.photo, types.Photo)))):
            raise TelegramResultError("Edited post could not be verified; reconcile before retrying")
        return _post_record(channel, message_id)

    async def post_exists(self, channel: dict, message_id: int) -> bool:
        message = await self.client.get_messages(_input_peer(channel), ids=message_id)
        return _matches_post(message, channel) and message.id == message_id

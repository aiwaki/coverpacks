"""Deterministic, local-only Telegram custom emoji preparation.

Original downloads are read-only. Each prepared pack is frozen: later selections
append new packs, and publishing can refer to one immutable manifest by hash.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import unicodedata
import warnings
from collections.abc import Mapping
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageOps, __version__ as pillow_version

from .state import State, collection_lock


SCHEMA_VERSION = 1
ADAPTER_VERSION = 1
EMOJI_SIZE = (100, 100)
MAX_ASSET_BYTES = 512 * 1024
MAX_SOURCE_BYTES = 30 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _immutable_write(path, content):
    """Install a complete new file without replacing an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Подготовленный файл изменён: {path}")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".prepare-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _replace_pointer(path, content):
    if path.exists() and path.read_bytes() == content:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".pointer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _selected(rows):
    selected, seen = [], set()
    for row in sorted(rows, key=lambda current: current["seq"]):
        if (row.get("status") != "downloaded" or row.get("excluded")
                or row.get("needs_review") or row.get("selected") is False):
            continue
        digest = row.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"У обложки #{row['seq']} отсутствует корректный SHA-256.")
        if digest not in seen:
            selected.append(row)
            seen.add(digest)
    return selected


def album_identity(row) -> tuple[str, str] | None:
    """Normalize known album metadata without removing editions or artist credits."""
    values = []
    for key in ("artist", "title"):
        value = row.get(key)
        if not isinstance(value, str):
            return None
        value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
        if not value:
            return None
        values.append(value)
    return values[0], values[1]


def _source_path(root, row):
    path_value = row.get("path")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"У обложки #{row['seq']} отсутствует исходный файл.")
    path = (root / path_value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Исходный файл #{row['seq']} находится вне каталога коллекции.")
    return path


def _read_source(root, row):
    path = _source_path(root, row)
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError(f"Исходный файл #{row['seq']} слишком большой.")
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Не удалось прочитать обложку #{row['seq']}: {path}") from error
    if _sha(content) != row["sha256"]:
        raise ValueError(f"SHA-256 исходной обложки #{row['seq']} не совпадает с каталогом.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as original:
                if original.width * original.height > MAX_SOURCE_PIXELS:
                    raise ValueError("слишком много пикселей")
                if getattr(original, "n_frames", 1) != 1:
                    raise ValueError("ожидалась статичная обложка")
                if row.get("width") and row["width"] != original.width:
                    raise ValueError("ширина отличается от каталога")
                if row.get("height") and row["height"] != original.height:
                    raise ValueError("высота отличается от каталога")
                original.load()
                image = ImageOps.exif_transpose(original)
                profile = original.info.get("icc_profile")
                if profile:
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                    output_mode = "RGBA" if "A" in image.getbands() else "RGB"
                    image = ImageCms.profileToProfile(
                        image, source_profile, ImageCms.createProfile("sRGB"),
                        outputMode=output_mode,
                    )
                image = image.convert("RGBA")
                image.info.clear()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, ImageCms.PyCMSError) as error:
        raise ValueError(f"Некорректное изображение #{row['seq']}: {error}") from error
    return path, image


def _item_metadata(row, source_path):
    return {"seq": row["seq"], "artist": row.get("artist"), "title": row.get("title"),
            "source": row.get("source"), "source_url": row.get("source_url"),
            "source_entity_id": row.get("source_entity_id") or row.get("entity_id"),
            "source_sha256": row["sha256"], "source_path": str(source_path),
            "width": 100, "height": 100}


def _render(image):
    resized = ImageOps.contain(image, EMOJI_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", EMOJI_SIZE, (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((100 - resized.width) // 2, (100 - resized.height) // 2))
    canvas.info.clear()
    variants = {}
    for extension, options in (
        ("webp", {"format": "WEBP", "lossless": True, "quality": 100, "method": 6, "exact": True}),
        ("png", {"format": "PNG", "optimize": False, "compress_level": 9}),
    ):
        output = io.BytesIO()
        canvas.save(output, **options)
        content = output.getvalue()
        if len(content) > MAX_ASSET_BYTES:
            raise ValueError(f"Emoji .{extension} превышает 512 КиБ.")
        with Image.open(io.BytesIO(content)) as check:
            check.load()
            if check.size != EMOJI_SIZE or check.convert("RGBA").tobytes() != canvas.tobytes():
                raise ValueError(f"Проверка сохранения .{extension} без потерь не пройдена.")
        variants[extension] = content
    return canvas, variants


def _preview(items, images):
    columns, cell_width, cell_height = 5, 228, 190
    margin, header = 20, 60
    rows = math.ceil(len(items) / columns)
    canvas = Image.new("RGB", (margin * 2 + columns * cell_width, header + rows * cell_height + margin), "#e9e7df")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 12), "SLEEVE / Telegram custom emoji", fill="#151515")
    draw.text((margin, 30), "Actual 100 x 100 + chat-size 32 x 32 / light and dark", fill="#414141")
    for index, item in enumerate(items):
        x = margin + (index % columns) * cell_width
        y = header + (index // columns) * cell_height
        image = images[item["source_sha256"]]
        for offset, background, foreground in ((0, "#ffffff", "#161616"), (110, "#1c2530", "#eeeeee")):
            draw.rectangle((x + offset, y, x + offset + 107, y + 160), fill=background)
            canvas.paste(image, (x + offset + 4, y + 4), image)
            small = image.resize((32, 32), Image.Resampling.LANCZOS)
            canvas.paste(small, (x + offset + 4, y + 116), small)
            draw.text((x + offset + 42, y + 124), "32 px", fill=foreground)
        draw.text((x + 3, y + 167), f"#{item['seq']} / {item['position']:02d}", fill="#151515")
    output = io.BytesIO()
    canvas.save(output, format="PNG", compress_level=9)
    return output.getvalue()


def _previous(output_dir):
    pointer_path = output_dir / "latest.json"
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_bytes())
        filename = pointer["manifest"]
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("недопустимый путь manifest")
        content = (output_dir / filename).read_bytes()
        if _sha(content) != pointer["sha256"]:
            raise ValueError("контрольная сумма manifest не совпадает")
        return json.loads(content)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Повреждён указатель подготовленной коллекции: {pointer_path}") from error


def prepare(state: State, task_id: int, output_dir: Path | None = None, *,
            pack_size: int = 200, prefix: str = "sleeve_vinyl", brand: str = "SLEEVE",
            archived_packs: Mapping[str, str] | None = None,
            max_new_items: int | None = None) -> Path:
    """Freeze approved downloads into packs and return an immutable manifest.

    Pack size is the capacity for newly prepared packs, up to Telegram's checked
    200-item limit. Changing capacity never expands or rearranges a frozen pack.
    Verified archived packs retain their payload without reading old image files.
    Their hashes use compact, sorted JSON; catalogue identity must still match.
    max_new_items caps additions per call; a caller owns any daily/retry journal.
    Network requests and Telegram publication are separate.
    """
    if isinstance(pack_size, bool) or not isinstance(pack_size, int) or not 1 <= pack_size <= 200:
        raise ValueError("Размер пака должен быть целым числом от 1 до 200.")
    if (not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,49}", prefix)
            or "__" in prefix or prefix.endswith("_")):
        raise ValueError("Префикс: латинская буква, затем буквы, цифры или одиночные подчёркивания; до 50 символов.")
    if not isinstance(brand, str) or not brand.strip() or len(brand) > 40 or any(ord(c) < 32 for c in brand):
        raise ValueError("Название серии должно содержать от 1 до 40 печатных символов.")
    if (max_new_items is not None and (isinstance(max_new_items, bool)
            or not isinstance(max_new_items, int) or max_new_items < 0)):
        raise ValueError("Лимит новых обложек должен быть целым неотрицательным числом.")
    if archived_packs is not None and not isinstance(archived_packs, Mapping):
        raise ValueError("Архивные паки должны быть сопоставлением short_name и SHA-256.")
    archived_packs = dict(archived_packs or {})
    if any(not isinstance(name, str) or not isinstance(digest, str)
           or not re.fullmatch(r"[0-9a-f]{64}", digest)
           for name, digest in archived_packs.items()):
        raise ValueError("Некорректное имя или SHA-256 архивного пака.")
    state.task(task_id)
    output_dir = (Path(output_dir) if output_dir is not None else state.root / "emoji" / prefix).resolve()
    with collection_lock(output_dir):
        return _prepare_locked(state, task_id, output_dir, pack_size, prefix, brand.strip(),
                               archived_packs, max_new_items)


def _prepare_locked(state, task_id, output_dir, pack_size, prefix, brand, archived_packs, max_new_items):
    rows = _selected(state.rows(task_id))
    if not rows:
        raise ValueError("Нет выбранных и проверенных обложек для подготовки emoji.")
    configuration = {"schema_version": SCHEMA_VERSION, "adapter_version": ADAPTER_VERSION,
                     "pillow_version": pillow_version, "task_id": task_id,
                     "brand": brand, "prefix": prefix, "pack_size": pack_size}
    previous = _previous(output_dir)
    # Capacity is a plan for future additions, not part of frozen pack identity.
    # A size-only change creates a new immutable manifest with identical packs.
    if previous and any(previous.get(key) != value for key, value in configuration.items() if key != "pack_size"):
        raise ValueError("Параметры серии изменились. Для новой серии укажите другой каталог output_dir.")
    packs = previous["packs"] if previous else []
    packs_by_name = {pack["short_name"]: pack for pack in packs}
    for name, expected_hash in archived_packs.items():
        pack = packs_by_name.get(name)
        if pack is None or _sha(json.dumps(pack, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False).encode()) != expected_hash:
            raise ValueError(f"Архивный пак {name} отсутствует в предыдущем manifest или его SHA-256 не совпадает.")
    archived_items = {item["source_sha256"] for name in archived_packs
                      for item in packs_by_name[name]["items"]}
    frozen = {item["source_sha256"]: item for pack in packs for item in pack["items"]}
    current = {row["sha256"]: row for row in rows}
    if not frozen.keys() <= current.keys():
        raise ValueError("Ранее подготовленная обложка исключена, изменена или отправлена на проверку. Замороженный пак не изменён; создайте новую серию либо восстановите выбор.")
    seen_albums = {identity for item in frozen.values() if (identity := album_identity(item)) is not None}
    new_rows = []
    for row in rows:
        if row["sha256"] in frozen:
            continue
        identity = album_identity(row)
        if identity is not None:
            if identity in seen_albums:
                continue
            seen_albums.add(identity)
        new_rows.append(row)
    if max_new_items is not None:
        new_rows = new_rows[:max_new_items]
    new_digests = {row["sha256"] for row in new_rows}
    if not packs and not new_rows:
        raise ValueError("Нет ранее подготовленных паков, а лимит новых обложек равен нулю.")
    next_number = max((pack["number"] for pack in packs), default=0) + 1
    planned_names = {}
    for start in range(0, len(new_rows), pack_size):
        number = next_number + start // pack_size
        title = f"{brand} · {number:03d} · @{prefix}"
        short_name = f"{prefix}_{number:03d}"
        if len(title) > 64 or len(short_name) > 64:
            raise ValueError("Название или короткое имя нового пака превышает 64 символа.")
        planned_names[number] = (title, short_name)
    assets, images, additions = [], {}, []
    for row in rows:
        digest = row["sha256"]
        if digest not in frozen and digest not in new_digests:
            continue
        if digest in archived_items:
            metadata = _item_metadata(row, _source_path(state.root, row))
            if any(frozen[digest].get(key) != value for key, value in metadata.items()):
                raise ValueError(f"Метаданные подготовленной обложки #{row['seq']} изменились. Старый manifest сохранён.")
            continue
        source_path, original = _read_source(state.root, row)
        image, variants = _render(original)
        images[digest] = image
        item = _item_metadata(row, source_path)
        for extension, content in variants.items():
            asset_path = output_dir / "assets" / f"{digest}.{extension}"
            item[f"{extension}_path"] = str(asset_path)
            item[f"{extension}_sha256"] = _sha(content)
            item[f"{extension}_bytes"] = len(content)
            assets.append((asset_path, content))
        if digest in frozen:
            # A frozen identity cannot silently be reassigned after catalogue edits.
            if any(frozen[digest].get(key) != value for key, value in item.items()):
                raise ValueError(f"Метаданные подготовленной обложки #{row['seq']} изменились. Старый manifest сохранён.")
        else:
            additions.append(item)
    for start in range(0, len(additions), pack_size):
        number = next_number + start // pack_size
        items = additions[start:start + pack_size]
        for position, item in enumerate(items, 1):
            item["position"] = position
        title, short_name = planned_names[number]
        message = (f"{title}\n\n{len(items)} обложек виниловых релизов в формате Telegram emoji.\n"
                   f"Полная обложка, без обрезки. Источники и треклист — в каталоге выпуска.\n\n"
                   f"Добавить пак: https://t.me/addemoji/{short_name}")
        packs.append({"number": number, "title": title, "short_name": short_name,
                      "emoji": "💿", "message": message, "items": items})
    all_items = [item for pack in packs for item in pack["items"]]
    if archived_packs and not additions:
        preview_content = None
        preview_path = Path(previous["preview_path"])
        preview_digest = previous["preview_sha256"]
    else:
        preview_content = _preview(additions if archived_packs else all_items, images)
        preview_digest = _sha(preview_content)
        preview_path = output_dir / f"preview-{preview_digest[:16]}.png"
    manifest = dict(configuration, packs=packs, preview_path=str(preview_path),
                    preview_sha256=preview_digest, item_count=len(all_items))
    manifest_content = _json_bytes(manifest)
    manifest_digest = _sha(manifest_content)
    manifest_path = output_dir / f"manifest-{manifest_digest[:16]}.json"
    # Validate the entire selection before installing anything; source files are never written.
    for path, content in assets:
        _immutable_write(path, content)
    if preview_content is not None:
        _immutable_write(preview_path, preview_content)
    _immutable_write(manifest_path, manifest_content)
    _replace_pointer(output_dir / "latest.json", _json_bytes({"manifest": manifest_path.name, "sha256": manifest_digest}))
    return manifest_path

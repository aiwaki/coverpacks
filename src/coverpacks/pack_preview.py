"""Print-like preview photos assembled from unchanged full-resolution covers."""
from __future__ import annotations

import hashlib
import io
import math
import re
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps

from .emoji import _immutable_write
from .state import collection_lock


MAX_COVERS_PER_PHOTO = 50
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_SOURCE_BYTES = 30 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000
BACKGROUND = (250, 249, 245)
INK = (20, 20, 18)
MARGIN = 48
GAP = 8
HEADER = 190


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _source(item):
    path_value, digest = item.get("source_path"), item.get("source_sha256")
    if (not isinstance(path_value, str) or not Path(path_value).is_absolute()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("Для превью нужны абсолютный путь исходника и SHA-256 из manifest.")
    try:
        with Path(path_value).open("rb") as handle:
            content = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as error:
        raise ValueError(f"Не удалось прочитать исходную обложку: {path_value}") from error
    if len(content) > MAX_SOURCE_BYTES or _sha(content) != digest:
        raise ValueError(f"Исходная обложка изменилась или превышает допустимый размер: {path_value}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as original:
                if original.width * original.height > MAX_SOURCE_PIXELS or getattr(original, "n_frames", 1) != 1:
                    raise ValueError("Ожидалась статичная обложка допустимого размера.")
                original.load()
                image = ImageOps.exif_transpose(original)
                profile = original.info.get("icc_profile")
                if profile:
                    source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
                    image = ImageCms.profileToProfile(
                        image, source_profile, ImageCms.createProfile("sRGB"),
                        outputMode="RGBA" if "A" in image.getbands() else "RGB",
                    )
                image = image.convert("RGBA")
                image.info.clear()
                return image
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, ImageCms.PyCMSError) as error:
        raise ValueError(f"Не удалось декодировать исходную обложку: {path_value}") from error


def _layout(count):
    columns = math.ceil(math.sqrt(count)) if count <= 25 else 10
    width = 1400 if count <= 25 else 2048
    tile = (width - 2 * MARGIN - (columns - 1) * GAP) // columns
    width = 2 * MARGIN + columns * tile + (columns - 1) * GAP
    rows = math.ceil(count / columns)
    height = HEADER + rows * tile + (rows - 1) * GAP + MARGIN
    return width, height, columns, tile


def _font_fitting(draw, text, size, available_width, *, stroke_width=0):
    # Pillow's bundled font avoids machine-specific installed font selection.
    while size >= 12:
        font = ImageFont.load_default(size=size)
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        if box[2] - box[0] <= available_width:
            return font
        size -= 1
    raise ValueError("Название не помещается в шапку превью.")


def _render_page(pack, items, channel_username, page, pages):
    width, height, columns, tile = _layout(len(items))
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    brand = pack["title"].split(" · ", 1)[0].strip()
    title = f"{brand} / {pack['number']:03d}"
    title_font = _font_fitting(draw, title, 76, width - 2 * MARGIN, stroke_width=1)
    draw.text((MARGIN, 34), title, font=title_font, fill=INK, stroke_width=1)
    handle = f"@{channel_username}"
    handle_font = _font_fitting(draw, handle, 28, width - 2 * MARGIN - 130)
    draw.text((MARGIN, 132), handle, font=handle_font, fill=INK)
    if pages > 1:
        counter = f"{page:02d} / {pages:02d}"
        counter_font = ImageFont.load_default(size=26)
        box = draw.textbbox((0, 0), counter, font=counter_font)
        draw.text((width - MARGIN - (box[2] - box[0]), 132), counter, font=counter_font, fill=INK)
    for index, item in enumerate(items):
        image = _source(item)
        fitted = ImageOps.contain(image, (tile, tile), Image.Resampling.LANCZOS)
        x = MARGIN + (index % columns) * (tile + GAP) + (tile - fitted.width) // 2
        y = HEADER + (index // columns) * (tile + GAP) + (tile - fitted.height) // 2
        canvas.paste(fitted, (x, y), fitted)
    output = io.BytesIO()
    # Full-resolution sources and 4:4:4 preserve small cover details locally.
    canvas.save(output, "JPEG", quality=95, subsampling=0, optimize=False, progressive=False)
    content = output.getvalue()
    if len(content) > MAX_PHOTO_BYTES or width + height > 10000 or max(width / height, height / width) > 20:
        raise ValueError("Превью превышает ограничения Telegram photo.")
    with Image.open(io.BytesIO(content)) as check:
        check.verify()
    return content, width, height


def generate_preview(pack: dict, output_dir: Path, channel_username: str, *,
                     page_sizes: list[int] | None = None) -> list[dict]:
    """Create deterministic high-quality JPEG sidecars, at most 50 covers each.

    Explicit page sizes can match caption budgets while preserving item order.
    All sources and pages are checked before any photo is installed. File names
    include a content hash; existing photos and the publication manifest are
    never rewritten. No network operations or Telegram publication occur here.
    """
    if (not isinstance(channel_username, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", channel_username)):
        raise ValueError("Для превью нужен username канала без @.")
    if (not isinstance(pack, dict) or type(pack.get("number")) is not int or pack["number"] < 1
            or not isinstance(pack.get("title"), str) or not pack["title"].strip()
            or not isinstance(pack.get("items"), list) or not 1 <= len(pack["items"]) <= 200):
        raise ValueError("Для превью нужен нумерованный пак из 1–200 обложек.")
    items = pack["items"]
    if any(not isinstance(item, dict) or item.get("position") != index for index, item in enumerate(items, 1)):
        raise ValueError("Порядок обложек в manifest повреждён.")
    if len({item.get("source_sha256") for item in items}) != len(items):
        raise ValueError("В preview-паке повторяются исходные обложки.")
    if page_sizes is None:
        sizes = [min(MAX_COVERS_PER_PHOTO, len(items) - start)
                 for start in range(0, len(items), MAX_COVERS_PER_PHOTO)]
    else:
        if (not isinstance(page_sizes, list) or not page_sizes
                or any(type(size) is not int or not 1 <= size <= MAX_COVERS_PER_PHOTO for size in page_sizes)
                or sum(page_sizes) != len(items)):
            raise ValueError("page_sizes должен содержать целые числа от 1 до 50 с суммой, равной числу обложек.")
        sizes = list(page_sizes)
    output_dir = Path(output_dir).resolve()
    with collection_lock(output_dir):
        pages = len(sizes)
        prepared = []
        start = 0
        for page, size in enumerate(sizes, 1):
            content, width, height = _render_page(pack, items[start:start + size], channel_username, page, pages)
            digest = _sha(content)
            path = output_dir / f"pack-{pack['number']:03d}-{page:02d}-{digest[:16]}.jpg"
            prepared.append((path, content, {"path": str(path), "sha256": digest, "width": width, "height": height}))
            start += size
        for path, content, _ in prepared:
            _immutable_write(path, content)
        return [metadata for _, _, metadata in prepared]

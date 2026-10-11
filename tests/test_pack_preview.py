"""Source-preserving photo sheets, independent of Telegram and network access."""
import hashlib
import io
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin

from coverpacks.pack_preview import BACKGROUND, GAP, HEADER, MARGIN, _layout, generate_preview


def make_pack(tmp_path, count=25):
    items = []
    for index in range(1, count + 1):
        image = Image.new("RGB", (160, 160), (index, (index * 37) % 255, (index * 67) % 255))
        output = io.BytesIO()
        image.save(output, "PNG")
        content = output.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        path = tmp_path / "sources" / f"{digest}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        items.append({"position": index, "source_path": str(path.resolve()), "source_sha256": digest,
                      "webp_path": str(tmp_path / "nonexistent-emoji.webp"), "title": f"Album {index}"})
    return {"number": 1, "title": "SLEEVE · 001 · @sleeve_vinyl", "items": items}


def close_color(actual, expected, tolerance=4):
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def test_25_cover_sheet_has_five_by_five_source_images_and_valid_photo(tmp_path):
    pack = make_pack(tmp_path)
    originals = {Path(item["source_path"]): (Path(item["source_path"]).read_bytes(), Path(item["source_path"]).stat().st_mtime_ns) for item in pack["items"]}
    result = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert len(result) == 1
    metadata = result[0]
    path = Path(metadata["path"])
    assert path.is_absolute() and path.is_file()
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
    assert len(content) < 10 * 1024 * 1024
    width, height, columns, tile = _layout(25)
    assert columns == 5 and (metadata["width"], metadata["height"]) == (width, height)
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.size == (width, height)
        assert JpegImagePlugin.get_sampling(image) == 0
        assert width + height <= 10000 and max(width / height, height / width) <= 20
        for index in range(25):
            center = (MARGIN + (index % columns) * (tile + GAP) + tile // 2,
                      HEADER + (index // columns) * (tile + GAP) + tile // 2)
            expected = (index + 1, ((index + 1) * 37) % 255, ((index + 1) * 67) % 255)
            close_color(image.getpixel(center), expected)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals} == originals


def test_200_cover_pack_uses_exactly_four_photos_with_at_most_50_each(tmp_path):
    pack = make_pack(tmp_path, 200)
    result = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert len(result) == 4
    assert len({item["sha256"] for item in result}) == 4
    for page, metadata in enumerate(result):
        width, height, columns, tile = _layout(50)
        assert columns == 10 and (metadata["width"], metadata["height"]) == (width, height)
        with Image.open(metadata["path"]) as image:
            for local in (0, 49):
                index = page * 50 + local + 1
                center = (MARGIN + (local % columns) * (tile + GAP) + tile // 2,
                          HEADER + (local // columns) * (tile + GAP) + tile // 2)
                close_color(image.getpixel(center), (index, (index * 37) % 255, (index * 67) % 255))
        assert Path(metadata["path"]).stat().st_size < 10 * 1024 * 1024


def test_partial_last_page_and_repeat_are_immutable(tmp_path):
    pack = make_pack(tmp_path, 51)
    first = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert len(first) == 2
    before = {item["path"]: (Path(item["path"]).read_bytes(), Path(item["path"]).stat().st_mtime_ns) for item in first}
    second = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert first == second
    assert {path: (Path(path).read_bytes(), Path(path).stat().st_mtime_ns) for path in before} == before


def test_explicit_page_sizes_preserve_item_groups_and_actual_page_counter(tmp_path, monkeypatch):
    from coverpacks import pack_preview
    pack = make_pack(tmp_path, 25)
    actual_render = pack_preview._render_page
    calls = []
    def render(current_pack, items, username, page, pages):
        calls.append(([item["position"] for item in items], page, pages))
        return actual_render(current_pack, items, username, page, pages)
    monkeypatch.setattr(pack_preview, "_render_page", render)
    result = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl", page_sizes=[8, 10, 7])
    assert len(result) == 3
    assert calls == [(list(range(1, 9)), 1, 3), (list(range(9, 19)), 2, 3), (list(range(19, 26)), 3, 3)]
    for metadata, (positions, _, _) in zip(result, calls):
        width, height, columns, tile = _layout(len(positions))
        assert (metadata["width"], metadata["height"]) == (width, height)
        with Image.open(metadata["path"]) as image:
            for local in (0, len(positions) - 1):
                index = positions[local]
                center = (MARGIN + (local % columns) * (tile + GAP) + tile // 2,
                          HEADER + (local // columns) * (tile + GAP) + tile // 2)
                close_color(image.getpixel(center), (index, (index * 37) % 255, (index * 67) % 255))


def test_explicit_sizes_equal_to_default_keep_identical_photo_and_mtime(tmp_path):
    pack = make_pack(tmp_path, 51)
    first = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    before = {item["path"]: Path(item["path"]).stat().st_mtime_ns for item in first}
    assert generate_preview(pack, tmp_path / "previews", "sleeve_vinyl", page_sizes=[50, 1]) == first
    assert {path: Path(path).stat().st_mtime_ns for path in before} == before


@pytest.mark.parametrize("sizes", [[], [25.0], [True, 24], [False, 25], [0, 25], [-1, 26],
                                   [51], [24], [26], (25,), "25", ["25"], [None]])
def test_invalid_explicit_sizes_fail_before_rendering_or_output(tmp_path, monkeypatch, sizes):
    pack = make_pack(tmp_path, 25)
    def unexpected_render(*args):
        pytest.fail("invalid page sizes must fail before rendering")
    monkeypatch.setattr("coverpacks.pack_preview._render_page", unexpected_render)
    with pytest.raises(ValueError, match="page_sizes"):
        generate_preview(pack, tmp_path / "previews", "sleeve_vinyl", page_sizes=sizes)
    assert not (tmp_path / "previews").exists()


def test_explicit_single_page_cannot_exceed_50_even_when_total_matches(tmp_path):
    pack = make_pack(tmp_path, 51)
    with pytest.raises(ValueError, match="page_sizes"):
        generate_preview(pack, tmp_path / "previews", "sleeve_vinyl", page_sizes=[51])
    assert not (tmp_path / "previews").exists()


def test_nonsquare_source_is_fitted_fully_without_crop_or_stretch(tmp_path):
    pack = make_pack(tmp_path, 1)
    source = Image.new("RGB", (200, 100), "green")
    source.paste("red", (0, 0, 40, 100))
    source.paste("blue", (160, 0, 200, 100))
    path = Path(pack["items"][0]["source_path"])
    source.save(path, "PNG")
    pack["items"][0]["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    output = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")[0]
    _, _, _, tile = _layout(1)
    top = HEADER + tile // 4
    with Image.open(output["path"]) as image:
        close_color(image.getpixel((MARGIN + 30, HEADER + 30)), BACKGROUND)
        close_color(image.getpixel((MARGIN + 30, top + 30)), (255, 0, 0))
        close_color(image.getpixel((MARGIN + tile - 30, top + 30)), (0, 0, 255))
        close_color(image.getpixel((MARGIN + tile // 2, top + tile // 4)), (0, 128, 0))
        close_color(image.getpixel((MARGIN + 30, HEADER + tile - 30)), BACKGROUND)


@pytest.mark.parametrize("damage", ["missing", "sha", "decode"])
def test_source_failure_on_later_page_installs_no_photos(tmp_path, damage):
    pack = make_pack(tmp_path, 51)
    item = pack["items"][-1]
    path = Path(item["source_path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged")
        if damage == "decode":
            item["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert not list((tmp_path / "previews").glob("*.jpg"))


def test_corrupt_existing_preview_is_not_overwritten(tmp_path):
    pack = make_pack(tmp_path, 1)
    metadata = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")[0]
    path = Path(metadata["path"])
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Подготовленный файл изменён"):
        generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")
    assert path.read_bytes() == b"changed"


def test_channel_or_issue_change_creates_separate_photo_without_mutating_old(tmp_path):
    pack = make_pack(tmp_path, 1)
    first = generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")[0]
    before = Path(first["path"]).read_bytes()
    second = generate_preview(pack, tmp_path / "previews", "other_channel")[0]
    assert second["sha256"] != first["sha256"]
    pack["number"] = 2
    third = generate_preview(pack, tmp_path / "previews", "other_channel")[0]
    assert third["sha256"] != second["sha256"]
    assert Path(first["path"]).read_bytes() == before


@pytest.mark.parametrize("username", ["@sleeve_vinyl", "../outside", "bad handle", "a" * 33, ""])
def test_invalid_channel_name_is_rejected_before_files(tmp_path, username):
    with pytest.raises(ValueError, match="username"):
        generate_preview(make_pack(tmp_path, 1), tmp_path / "previews", username)
    assert not (tmp_path / "previews").exists()


@pytest.mark.parametrize("change", ["empty", "order", "duplicates", "too_many"])
def test_invalid_pack_is_rejected(tmp_path, change):
    pack = make_pack(tmp_path, 2)
    if change == "empty":
        pack["items"] = []
    elif change == "order":
        pack["items"].reverse()
    elif change == "duplicates":
        pack["items"][1]["source_sha256"] = pack["items"][0]["source_sha256"]
    else:
        pack["items"] = pack["items"] * 101
    with pytest.raises(ValueError):
        generate_preview(pack, tmp_path / "previews", "sleeve_vinyl")

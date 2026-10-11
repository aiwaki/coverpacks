"""Static Telegram asset contract and frozen publication planning, offline."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from coverpacks.emoji import album_identity, prepare
from coverpacks.state import State


class Collection:
    def __init__(self, root):
        self.root = root.resolve()
        self.entries = []

    def task(self, task_id):
        if task_id != 2:
            raise ValueError("Задача не найдена")
        return {"id": task_id}

    def rows(self, task_id):
        return list(self.entries)


@pytest.fixture
def collection(tmp_path):
    return Collection(tmp_path / "data")


def add_image(collection, seq, size=(120, 120), color=None, **changes):
    color = color or (seq % 255, 100, 200, 255)
    image = Image.new("RGBA", size, color)
    # Distinct edges reveal accidental cropping or stretching.
    if size[0] > 1:
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.putpixel((size[0] - 1, size[1] - 1), (0, 255, 0, 255))
    output = io.BytesIO()
    image.save(output, "PNG")
    content = output.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    path = collection.root / "covers" / f"{digest}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    row = {"seq": seq, "task_id": 2, "status": "downloaded", "excluded": False,
           "needs_review": False, "path": str(path.relative_to(collection.root)),
           "sha256": digest, "width": size[0], "height": size[1],
           "artist": f"Artist {seq}", "title": f"Album {seq}", "source": "bandcamp",
           "source_url": f"https://artist{seq}.bandcamp.com/album/album",
           "entity_id": f"bandcamp:album:{seq}"}
    row.update(changes)
    collection.entries.append(row)
    return row


def manifest(path):
    return json.loads(path.read_bytes())


def archive_hashes(prepared):
    return {pack["short_name"]: hashlib.sha256(json.dumps(
        pack, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest() for pack in prepared["packs"]}


def test_static_formats_dimensions_lossless_and_originals_untouched(collection):
    row = add_image(collection, 56)
    source = collection.root / row["path"]
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    prepared = manifest(prepare(collection, 2))
    item = prepared["packs"][0]["items"][0]
    assert item["seq"] == 56 and item["position"] == 1
    assert prepared["packs"][0]["title"] == "SLEEVE · 001 · @sleeve_vinyl"
    assert prepared["packs"][0]["short_name"] == "sleeve_vinyl_001"
    assert item["source_path"] == str(source)
    assert item["source_sha256"] == row["sha256"]
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    with Image.open(item["png_path"]) as png, Image.open(item["webp_path"]) as webp:
        assert png.size == webp.size == (100, 100)
        assert png.format == "PNG" and webp.format == "WEBP"
        assert png.convert("RGBA").tobytes() == webp.convert("RGBA").tobytes()
    for extension in ("png", "webp"):
        content = Path(item[f"{extension}_path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item[f"{extension}_sha256"]
        assert len(content) == item[f"{extension}_bytes"] < 512 * 1024
    assert Path(prepared["preview_path"]).is_file()


def test_repeat_is_identical_and_does_not_rewrite_files(collection):
    add_image(collection, 1)
    add_image(collection, 2)
    first = prepare(collection, 2)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in first.parent.rglob("*") if path.is_file() and path.name != ".collector.lock"}
    second = prepare(collection, 2)
    assert first == second
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before
    pointer = json.loads((first.parent / "latest.json").read_bytes())
    assert pointer["manifest"] == first.name
    assert pointer["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()


@pytest.mark.parametrize("size,expected_box", [((200, 100), (0, 25, 100, 75)), ((100, 200), (25, 0, 75, 100))])
def test_nonsquare_preserves_full_art_with_centered_transparent_padding(collection, size, expected_box):
    add_image(collection, 1, size=size)
    item = manifest(prepare(collection, 2))["packs"][0]["items"][0]
    with Image.open(item["png_path"]) as image:
        assert image.getbbox() == expected_box
        assert image.getpixel((0, 0)) == (0, 0, 0, 0)
        left, top, right, bottom = expected_box
        assert image.getpixel((left, top))[0] > 0
        assert image.getpixel((right - 1, bottom - 1))[1] > 0


def test_selection_sorting_and_sha_dedup_preserve_first_selected_identity(collection):
    add_image(collection, 9)
    add_image(collection, 4, excluded=True)
    add_image(collection, 5, needs_review=True)
    add_image(collection, 6, status="needs_review")
    add_image(collection, 7, selected=False)
    add_image(collection, 8, status="pending")
    add_image(collection, 3)
    duplicate = dict(collection.entries[0], seq=10)
    collection.entries.append(duplicate)
    items = manifest(prepare(collection, 2))["packs"][0]["items"]
    assert [item["seq"] for item in items] == [3, 9]
    assert [item["position"] for item in items] == [1, 2]


def test_new_album_dedup_ignores_label_case_unicode_and_whitespace_variations(collection):
    first = add_image(collection, 1, artist="Ｎｏｏｔｈｇｒｕｓｈ", title="Violations Of Consensual Reality",
                      source_url="https://label.bandcamp.com/album/violations")
    duplicate = add_image(collection, 2, artist=" noothgrush\t", title="violations\u00a0of  consensual\nreality",
                          source_url="https://artist.bandcamp.com/album/violations")
    add_image(collection, 3)
    assert first["sha256"] != duplicate["sha256"]
    assert album_identity(first) == album_identity(duplicate) == ("noothgrush", "violations of consensual reality")
    prepared = manifest(prepare(collection, 2, pack_size=1))
    assert [[item["seq"] for item in pack["items"]] for pack in prepared["packs"]] == [[1], [3]]
    assert all(row["status"] == "downloaded" and not row["excluded"] for row in collection.entries)


def test_new_album_dedup_respects_frozen_album_identity_without_rewriting_previous_pack(collection):
    add_image(collection, 1, artist="Noothgrush", title="Violations Of Consensual Reality")
    first = prepare(collection, 2)
    original = first.read_bytes()
    frozen_pack = manifest(first)["packs"][0]
    duplicate = add_image(collection, 2, artist="noothgrush", title="Violations of Consensual Reality")
    # A skipped addition requires no source/derived-art read or rewrite.
    (collection.root / duplicate["path"]).unlink()
    assert prepare(collection, 2) == first
    add_image(collection, 3)
    prepared = manifest(prepare(collection, 2))
    assert prepared["packs"][0] == frozen_pack and first.read_bytes() == original
    assert [item["seq"] for item in prepared["packs"][1]["items"]] == [3]


def test_new_album_dedup_preserves_distinct_artists_editions_and_feature_credits(collection):
    for seq, artist, title in [(1, "Artist", "Album"), (2, "Other Artist", "Album"),
                               (3, "Artist", "Album (Deluxe Edition)"), (4, "Artist feat. Guest", "Album")]:
        add_image(collection, seq, artist=artist, title=title)
    items = manifest(prepare(collection, 2))["packs"][0]["items"]
    assert [item["seq"] for item in items] == [1, 2, 3, 4]


@pytest.mark.parametrize("artist,title", [(None, "Album"), ("", "Album"), (" \t", "Album"),
                                          ("Artist", None), ("Artist", ""), ("Artist", "\n "),
                                          (123, "Album")])
def test_unknown_album_metadata_is_not_a_dedup_identity(collection, artist, title):
    first = add_image(collection, 1, artist=artist, title=title)
    add_image(collection, 2, artist=artist, title=title)
    assert album_identity(first) is None
    items = manifest(prepare(collection, 2))["packs"][0]["items"]
    assert [item["seq"] for item in items] == [1, 2]


def test_album_dedup_happens_before_new_item_capacity(collection):
    add_image(collection, 1, artist="Artist", title="Album")
    add_image(collection, 2, artist="ARTIST", title="ALBUM")
    add_image(collection, 3)
    add_image(collection, 4)
    first = prepare(collection, 2, max_new_items=2)
    assert [item["seq"] for item in manifest(first)["packs"][0]["items"]] == [1, 3]
    second = manifest(prepare(collection, 2, max_new_items=2))
    assert [item["seq"] for item in second["packs"][1]["items"]] == [4]


def test_album_dedup_does_not_rewrite_legacy_duplicates_or_relax_frozen_metadata(collection, monkeypatch):
    first_row = add_image(collection, 1, artist="Artist", title="Album")
    add_image(collection, 2, artist="ARTIST", title="ALBUM")
    with monkeypatch.context() as previous_behavior:
        previous_behavior.setattr("coverpacks.emoji.album_identity", lambda row: None)
        first = prepare(collection, 2)
    original = first.read_bytes()
    assert [item["seq"] for item in manifest(first)["packs"][0]["items"]] == [1, 2]
    add_image(collection, 3, artist="artist", title="album")
    assert prepare(collection, 2) == first and first.read_bytes() == original
    first_row["title"] = "ALBUM"
    with pytest.raises(ValueError, match="Метаданные"):
        prepare(collection, 2)
    assert first.read_bytes() == original


def test_expansion_keeps_existing_partial_pack_frozen(collection):
    add_image(collection, 1)
    first_path = prepare(collection, 2, pack_size=2)
    first_bytes = first_path.read_bytes()
    original_pack = manifest(first_path)["packs"][0]
    add_image(collection, 2)
    add_image(collection, 3)
    add_image(collection, 4)
    next_path = prepare(collection, 2, pack_size=2)
    next_manifest = manifest(next_path)
    assert first_path != next_path and first_path.read_bytes() == first_bytes
    assert next_manifest["packs"][0] == original_pack
    assert [pack["number"] for pack in next_manifest["packs"]] == [1, 2, 3]
    assert [[item["seq"] for item in pack["items"]] for pack in next_manifest["packs"]] == [[1], [2, 3], [4]]
    assert next_manifest["item_count"] == 4
    assert prepare(collection, 2, pack_size=2) == next_path


def test_default_capacity_200_does_not_split_at_old_editorial_25(collection):
    for seq in range(1, 27):
        add_image(collection, seq)
    prepared = manifest(prepare(collection, 2))
    assert prepared["pack_size"] == 200
    assert len(prepared["packs"]) == 1
    assert len(prepared["packs"][0]["items"]) == 26


def test_capacity_migration_keeps_frozen_25_unchanged_and_only_grows_future_packs(collection):
    for seq in range(1, 26):
        add_image(collection, seq)
    first_path = prepare(collection, 2, pack_size=25)
    first_bytes, first_mtime = first_path.read_bytes(), first_path.stat().st_mtime_ns
    first_pack = manifest(first_path)["packs"][0]
    original_assets = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                       for path in (first_path.parent / "assets").iterdir()}

    # Existing selection only: changing capacity must not create or expand a pack.
    migrated_path = prepare(collection, 2)
    migrated = manifest(migrated_path)
    assert migrated_path != first_path
    assert migrated["pack_size"] == 200
    assert migrated["packs"] == [first_pack]
    assert migrated["item_count"] == 25
    assert first_path.read_bytes() == first_bytes and first_path.stat().st_mtime_ns == first_mtime
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in original_assets} == original_assets
    assert prepare(collection, 2) == migrated_path

    # Later, explicitly collected additions use the new capacity in a new pack.
    for seq in range(26, 52):
        add_image(collection, seq)
    expanded = manifest(prepare(collection, 2))
    assert expanded["pack_size"] == 200
    assert len(expanded["packs"]) == 2
    assert expanded["packs"][0] == first_pack
    assert expanded["packs"][1]["number"] == 2
    assert expanded["packs"][1]["short_name"] == "sleeve_vinyl_002"
    assert [item["seq"] for item in expanded["packs"][1]["items"]] == list(range(26, 52))


def test_smaller_future_capacity_cannot_shrink_an_existing_frozen_pack(collection):
    for seq in range(1, 4):
        add_image(collection, seq)
    original = manifest(prepare(collection, 2, pack_size=3))["packs"][0]
    migrated = manifest(prepare(collection, 2, pack_size=1))
    assert migrated["pack_size"] == 1
    assert migrated["packs"] == [original]


def test_new_titles_include_channel_without_renaming_legacy_frozen_pack(collection):
    add_image(collection, 1)
    first = prepare(collection, 2)
    legacy = manifest(first)
    pack = legacy["packs"][0]
    pack["message"] = pack["message"].replace(pack["title"], "SLEEVE · 001")
    pack["title"] = "SLEEVE · 001"
    # Install a genuine old-shape frozen fixture and its matching pointer.
    content = (json.dumps(legacy, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    legacy_path = first.parent / f"manifest-{digest[:16]}.json"
    legacy_path.write_bytes(content)
    (first.parent / "latest.json").write_text(json.dumps({"manifest": legacy_path.name, "sha256": digest}))

    assert manifest(prepare(collection, 2))["packs"] == [pack]
    add_image(collection, 2)
    expanded = manifest(prepare(collection, 2))
    assert expanded["packs"][0] == pack
    assert expanded["packs"][1]["title"] == "SLEEVE · 002 · @sleeve_vinyl"
    assert expanded["packs"][1]["short_name"] == "sleeve_vinyl_002"
    assert legacy_path.read_bytes() == content


def test_new_title_over_64_characters_fails_before_rendering(collection, monkeypatch):
    add_image(collection, 1)
    def unexpected_render(image):
        pytest.fail("images must not be generated before checking pack titles")
    monkeypatch.setattr("coverpacks.emoji._render", unexpected_render)
    with pytest.raises(ValueError, match="превышает 64"):
        prepare(collection, 2, brand="B" * 40, prefix="s" * 15)
    assert not list((collection.root / "emoji").rglob("*.webp"))
    assert not list((collection.root / "emoji").rglob("manifest-*.json"))


def test_new_title_exactly_64_characters_is_accepted(collection):
    add_image(collection, 1)
    prepared = manifest(prepare(collection, 2, brand="B" * 40, prefix="s" * 14))
    title = prepared["packs"][0]["title"]
    assert len(title) == 64
    assert title.endswith("@" + "s" * 14)


@pytest.mark.parametrize("change", [{"excluded": True}, {"needs_review": True}, {"title": "Changed"}, {"selected": False}])
def test_frozen_selection_changes_are_detected_without_changing_manifest(collection, change):
    row = add_image(collection, 1)
    first_path = prepare(collection, 2)
    before = first_path.read_bytes()
    pointer = (first_path.parent / "latest.json").read_bytes()
    row.update(change)
    with pytest.raises(ValueError):
        prepare(collection, 2)
    assert first_path.read_bytes() == before
    assert (first_path.parent / "latest.json").read_bytes() == pointer


@pytest.mark.parametrize("damage", ["missing", "sha", "invalid", "dimensions"])
def test_invalid_source_stops_before_installing_assets(collection, damage):
    add_image(collection, 1)
    row = add_image(collection, 2)
    source = collection.root / row["path"]
    if damage == "missing":
        source.unlink()
    elif damage == "sha":
        source.write_bytes(b"wrong bytes")
    elif damage == "invalid":
        source.write_bytes(b"not an image")
        row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        row["width"] += 1
    with pytest.raises(ValueError):
        prepare(collection, 2)
    assert not list((collection.root / "emoji").rglob("*.webp"))
    assert not list((collection.root / "emoji").rglob("manifest-*.json"))


def test_existing_derived_asset_corruption_is_never_silently_overwritten(collection):
    add_image(collection, 1)
    path = prepare(collection, 2)
    asset = Path(manifest(path)["packs"][0]["items"][0]["webp_path"])
    asset.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Подготовленный файл изменён"):
        prepare(collection, 2)
    assert asset.read_bytes() == b"changed"


def test_corrupt_manifest_pointer_fails_closed(collection):
    add_image(collection, 1)
    path = prepare(collection, 2)
    path.write_text("{}")
    with pytest.raises(ValueError, match="Повреждён указатель"):
        prepare(collection, 2)


def test_manifest_pointer_cannot_escape_output_directory(collection):
    add_image(collection, 1)
    path = prepare(collection, 2)
    (path.parent / "latest.json").write_text(json.dumps({"manifest": "../outside.json", "sha256": "abc"}))
    with pytest.raises(ValueError, match="Повреждён указатель"):
        prepare(collection, 2)


def test_source_symlink_outside_collection_is_rejected(collection, tmp_path):
    row = add_image(collection, 1)
    source = collection.root / row["path"]
    outside = tmp_path / "outside.png"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ValueError, match="вне каталога"):
        prepare(collection, 2)


@pytest.mark.parametrize("parameter", [{"pack_size": 0}, {"pack_size": True}, {"pack_size": 201}, {"prefix": "../escape"}, {"prefix": "a__b"}, {"prefix": "a_"}, {"brand": ""}, {"brand": "a\nb"}])
def test_invalid_options_rejected(collection, parameter):
    add_image(collection, 1)
    with pytest.raises(ValueError):
        prepare(collection, 2, **parameter)


@pytest.mark.parametrize("parameter", [{"brand": "OTHER"}, {"prefix": "other"}])
def test_cannot_reconfigure_frozen_series_in_same_directory(collection, parameter):
    add_image(collection, 1)
    path = prepare(collection, 2)
    with pytest.raises(ValueError, match="Параметры серии изменились"):
        prepare(collection, 2, path.parent, **parameter)


def test_real_state_integration_does_not_mutate_database(collection, tmp_path):
    row = add_image(collection, 56)
    state = State(collection.root)
    try:
        task = state.create_task({"source": "bandcamp", "mode": "vinyl"})
        state.stage_bandcamp_page(task["id"], [{"id": "bandcamp:album:1:2", "title": "An album", "band_name": "An artist"}], "next")
        entry = state.rows(task["id"])[0]
        state.success(entry["seq"], dict(row, source_entity_id="bandcamp:album:1:2", source_image_id="2", format="PNG"))
        before = state.rows(task["id"])
        prepared = manifest(prepare(state, task["id"]))
        assert prepared["item_count"] == 1
        assert prepared["packs"][0]["items"][0]["source_entity_id"] == "bandcamp:album:1:2"
        assert state.rows(task["id"]) == before
    finally:
        state.close()


def test_verified_archive_preserves_payload_and_preview_without_reading_images(collection, monkeypatch):
    row = add_image(collection, 1)
    first = prepare(collection, 2)
    prepared = manifest(first)
    original_bytes = first.read_bytes()
    preview = Path(prepared["preview_path"])
    preview_before = (preview.read_bytes(), preview.stat().st_mtime_ns)
    item = prepared["packs"][0]["items"][0]
    (collection.root / row["path"]).unlink()
    Path(item["png_path"]).unlink()
    Path(item["webp_path"]).unlink()

    def unexpected(*_args):
        pytest.fail("archived packs must not read or regenerate images or preview")

    monkeypatch.setattr("coverpacks.emoji._read_source", unexpected)
    monkeypatch.setattr("coverpacks.emoji._render", unexpected)
    monkeypatch.setattr("coverpacks.emoji._preview", unexpected)
    assert prepare(collection, 2, archived_packs=archive_hashes(prepared)) == first
    assert first.read_bytes() == original_bytes
    assert (preview.read_bytes(), preview.stat().st_mtime_ns) == preview_before
    assert not Path(item["png_path"]).exists()
    assert not Path(item["webp_path"]).exists()


@pytest.mark.parametrize("invalid", ["wrong_hash", "unknown_pack", "malformed_hash"])
def test_archive_hash_must_match_previous_frozen_pack(collection, monkeypatch, invalid):
    add_image(collection, 1)
    first = prepare(collection, 2)
    archive = archive_hashes(manifest(first))
    if invalid == "wrong_hash":
        archive["sleeve_vinyl_001"] = "0" * 64
    elif invalid == "unknown_pack":
        archive["sleeve_vinyl_999"] = archive["sleeve_vinyl_001"]
    else:
        archive["sleeve_vinyl_001"] = "not-a-digest"
    pointer = (first.parent / "latest.json").read_bytes()

    def unexpected(*_args):
        pytest.fail("archive hashes must be validated before reading images")

    monkeypatch.setattr("coverpacks.emoji._read_source", unexpected)
    with pytest.raises(ValueError, match="[Аа]рхив"):
        prepare(collection, 2, archived_packs=archive)
    assert (first.parent / "latest.json").read_bytes() == pointer


def test_archive_cannot_authorize_pack_without_previous_manifest(collection):
    add_image(collection, 1)
    with pytest.raises(ValueError, match="Архивный пак"):
        prepare(collection, 2, archived_packs={"sleeve_vinyl_001": "0" * 64})
    assert not list((collection.root / "emoji").rglob("manifest-*.json"))


@pytest.mark.parametrize("change", [
    {"artist": "Другой исполнитель"}, {"title": "Другой альбом"},
    {"source": "other"}, {"source_url": "https://other.bandcamp.com/album/other"},
    {"source_entity_id": "bandcamp:album:other"}, {"seq": 8},
    {"path": "covers/another.png"}, {"sha256": "0" * 64},
    {"excluded": True}, {"needs_review": True}, {"selected": False},
    {"status": "pending"},
])
def test_archived_metadata_or_selection_drift_fails_without_changing_manifest(collection, change):
    row = add_image(collection, 1)
    add_image(collection, 2)
    first = prepare(collection, 2)
    prepared = manifest(first)
    before = first.read_bytes()
    pointer = (first.parent / "latest.json").read_bytes()
    (collection.root / row["path"]).unlink()
    row.update(change)
    with pytest.raises(ValueError):
        prepare(collection, 2, archived_packs=archive_hashes(prepared))
    assert first.read_bytes() == before
    assert (first.parent / "latest.json").read_bytes() == pointer


def test_archived_original_cannot_be_replaced_by_selected_duplicate(collection):
    row = add_image(collection, 1)
    first = prepare(collection, 2)
    archive = archive_hashes(manifest(first))
    collection.entries.append(dict(row, seq=2))
    row["excluded"] = True
    with pytest.raises(ValueError, match="Метаданные"):
        prepare(collection, 2, archived_packs=archive)


def test_archive_preview_contains_only_new_items_and_repeat_reuses_it(collection, monkeypatch):
    from coverpacks import emoji

    old = add_image(collection, 1)
    first = prepare(collection, 2)
    initial = manifest(first)
    archive = archive_hashes(initial)
    (collection.root / old["path"]).unlink()
    add_image(collection, 2)
    add_image(collection, 3)
    preview_calls = []
    original_preview = emoji._preview

    def capture(items, images):
        preview_calls.append([item["seq"] for item in items])
        return original_preview(items, images)

    monkeypatch.setattr(emoji, "_preview", capture)
    updated_path = prepare(collection, 2, archived_packs=archive)
    updated = manifest(updated_path)
    assert updated["packs"][0] == initial["packs"][0]
    assert [item["seq"] for item in updated["packs"][1]["items"]] == [2, 3]
    assert updated["item_count"] == 3
    assert preview_calls == [[2, 3]]
    assert updated["preview_path"] != initial["preview_path"]
    assert prepare(collection, 2, archived_packs=archive) == updated_path
    assert preview_calls == [[2, 3]]


@pytest.mark.parametrize("damage", ["missing", "sha", "invalid", "dimensions"])
def test_new_source_keeps_full_validation_beside_verified_archive(collection, damage):
    old = add_image(collection, 1)
    first = prepare(collection, 2)
    archive = archive_hashes(manifest(first))
    (collection.root / old["path"]).unlink()
    row = add_image(collection, 2)
    source = collection.root / row["path"]
    if damage == "missing":
        source.unlink()
    elif damage == "sha":
        source.write_bytes(b"modified")
    elif damage == "invalid":
        source.write_bytes(b"not an image")
        row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        row["height"] += 1
    pointer = (first.parent / "latest.json").read_bytes()
    assets = set((first.parent / "assets").iterdir())
    with pytest.raises(ValueError):
        prepare(collection, 2, archived_packs=archive)
    assert (first.parent / "latest.json").read_bytes() == pointer
    assert set((first.parent / "assets").iterdir()) == assets


def test_unarchived_frozen_source_still_requires_original(collection):
    first_row = add_image(collection, 1)
    second_row = add_image(collection, 2)
    first = prepare(collection, 2, pack_size=1)
    prepared = manifest(first)
    archive = {"sleeve_vinyl_001": archive_hashes(prepared)["sleeve_vinyl_001"]}
    (collection.root / first_row["path"]).unlink()
    (collection.root / second_row["path"]).unlink()
    with pytest.raises(ValueError, match="прочитать обложку #2"):
        prepare(collection, 2, pack_size=1, archived_packs=archive)


def test_cap_keeps_remaining_items_for_later_calls_without_mutating_collection(collection):
    for seq in range(1, 6):
        add_image(collection, seq)
    before = [dict(row) for row in collection.entries]
    first = prepare(collection, 2, max_new_items=2)
    first_manifest = manifest(first)
    assert first_manifest["item_count"] == 2
    assert [item["seq"] for item in first_manifest["packs"][0]["items"]] == [1, 2]
    assert collection.entries == before
    assert prepare(collection, 2, max_new_items=0) == first

    second = manifest(prepare(collection, 2, max_new_items=2))
    assert second["packs"][0] == first_manifest["packs"][0]
    assert [item["seq"] for item in second["packs"][1]["items"]] == [3, 4]
    third = prepare(collection, 2, max_new_items=2)
    assert [[item["seq"] for item in pack["items"]] for pack in manifest(third)["packs"]] == [[1, 2], [3, 4], [5]]
    assert prepare(collection, 2, max_new_items=2) == third
    assert collection.entries == before

    add_image(collection, 6)
    final = manifest(prepare(collection, 2, max_new_items=2))
    assert final["packs"][:3] == manifest(third)["packs"]
    assert [item["seq"] for item in final["packs"][3]["items"]] == [6]


def test_cap_does_not_read_deferred_sources_with_archived_packs(collection):
    old = add_image(collection, 1)
    first = prepare(collection, 2)
    archive = archive_hashes(manifest(first))
    (collection.root / old["path"]).unlink()
    add_image(collection, 2)
    deferred = add_image(collection, 3)
    (collection.root / deferred["path"]).unlink()
    second = prepare(collection, 2, archived_packs=archive, max_new_items=1)
    assert manifest(second)["item_count"] == 2
    assert prepare(collection, 2, archived_packs=archive, max_new_items=0) == second
    with pytest.raises(ValueError, match="прочитать обложку #3"):
        prepare(collection, 2, archived_packs=archive, max_new_items=1)


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "2"])
def test_invalid_new_item_cap_fails_before_rendering(collection, value):
    add_image(collection, 1)
    with pytest.raises(ValueError, match="Лимит новых"):
        prepare(collection, 2, max_new_items=value)
    assert not list((collection.root / "emoji").rglob("manifest-*.json"))


def test_zero_new_item_cap_cannot_create_empty_first_manifest(collection):
    add_image(collection, 1)
    with pytest.raises(ValueError, match="лимит новых обложек равен нулю"):
        prepare(collection, 2, max_new_items=0)


def test_no_archive_authorization_keeps_original_required_by_default(collection):
    row = add_image(collection, 1)
    prepare(collection, 2)
    (collection.root / row["path"]).unlink()
    for archive in (None, {}):
        with pytest.raises(ValueError, match="прочитать обложку #1"):
            prepare(collection, 2, archived_packs=archive)

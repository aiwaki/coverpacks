from html.parser import HTMLParser
import json
from pathlib import Path

import pytest
from PIL import Image

from coverpacks.presentation import export, gallery


class Elements(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def record(seq, **changes):
    return {
        "seq": seq,
        "artist": "Исполнитель",
        "title": f"Альбом {seq}",
        "status": "downloaded",
        "path": f"covers/{seq}.jpg",
        "thumbnail_path": f"thumbnails/{seq}.jpg",
        "width": 1200,
        "height": 1180,
        "format": "JPEG",
        "source_url": "https://musicbrainz.org/release/example",
        "excluded": False,
        "needs_review": False,
        **changes,
    }


def seqs(path):
    parsed = Elements(path.read_text())
    return [int(attrs["data-seq"]) for tag, attrs in parsed.tags if tag == "article"]


def write_image(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), "blue").save(path, format="JPEG")


def test_export_preserves_all_fields_nulls_unicode_and_discovery_order(tmp_path):
    rows = [record(12, unknown=None, arbitrary={"keep": [1, 2]}), record(2)]
    result = export(tmp_path, rows)
    assert result == tmp_path / "exports" / "manifest.jsonl"
    assert [json.loads(line) for line in result.read_text().splitlines()] == [rows[1], rows[0]]
    assert "Исполнитель" in result.read_text()
    assert not result.with_name(result.name + ".part").exists()


def test_failed_export_keeps_previous_manifest(tmp_path):
    path = export(tmp_path, [record(1)])
    previous = path.read_bytes()
    with pytest.raises(TypeError):
        export(tmp_path, [record(2, invalid=object())])
    assert path.read_bytes() == previous
    assert not list(path.parent.glob("*.part"))


def test_gallery_paginates_sorted_records_and_filtered_views(tmp_path):
    rows = [record(7, excluded=True, needs_review=True), record(3, needs_review=True), record(1), record(5, needs_review=True)]
    index = gallery(tmp_path, rows, page_size=2)
    assert seqs(index) == [1, 3]
    assert seqs(index.parent / "page-0002.html") == [5]
    assert seqs(index.parent / "needs_review.html") == [3, 5]
    assert seqs(index.parent / "excluded.html") == [7]
    assert 'href="page-0002.html" rel="next"' in index.read_text()
    assert 'href="index.html" rel="prev"' in (index.parent / "page-0002.html").read_text()
    assert "1200 × 1180 px" in index.read_text()


def test_gallery_escapes_metadata_and_rejects_unsafe_links(tmp_path):
    attack = '<script>alert("x")</script>'
    index = gallery(tmp_path, [record(1, artist=attack, title=attack,
        last_error=attack, review_reasons=[attack],
        source_url="javascript:alert(1)", image_url="data:text/html,bad",
        path="../../secret", thumbnail_path="/private/secret")])
    content = index.read_text()
    parsed = Elements(content)
    assert "&lt;script&gt;" in content
    assert not any(tag == "script" for tag, _ in parsed.tags)
    assert not any(tag == "img" for tag, _ in parsed.tags)
    hrefs = [attrs["href"] for tag, attrs in parsed.tags if tag == "a"]
    assert not any("secret" in href or "javascript:" in href or "data:" in href for href in hrefs)


def test_gallery_links_local_filenames_with_spaces_and_unicode(tmp_path):
    write_image(tmp_path, 'covers/Альбом "1".jpg')
    write_image(tmp_path, "thumbnails/1.jpg")
    index = gallery(tmp_path, [record(1, path='covers/Альбом "1".jpg', source_url='https://example.org/?a=1&b="2"')])
    parsed = Elements(index.read_text())
    links = [attrs["href"] for tag, attrs in parsed.tags if tag == "a"]
    assert "../covers/%D0%90%D0%BB%D1%8C%D0%B1%D0%BE%D0%BC%20%221%22.jpg" in links
    assert 'https://example.org/?a=1&b="2"' in links
    images = [attrs["src"] for tag, attrs in parsed.tags if tag == "img"]
    assert images == ["../thumbnails/1.jpg"]


def test_gallery_keeps_candidates_and_duplicates_out_of_cover_views(tmp_path):
    rows = [record(1), record(2, status="needs_review", needs_review=True)]
    for seq, status in enumerate(("pending", "not_found", "no_front", "network_error", "duplicate_group", "duplicate_image"), 3):
        rows.append(record(seq, status=status, needs_review=True, path=None, thumbnail_path=None))
    for row in rows[:2]:
        write_image(tmp_path, row["path"])
        write_image(tmp_path, row["thumbnail_path"])
    index = gallery(tmp_path, rows)
    assert seqs(index) == [1, 2]
    assert seqs(index.parent / "needs_review.html") == [2]
    assert "Превью недоступно" not in index.read_text()
    journal = Elements((index.parent / "journal.html").read_text())
    assert [int(attrs["data-seq"]) for tag, attrs in journal.tags if tag == "tr" and "data-seq" in attrs] == [3, 4, 5, 6, 7, 8]
    assert not any(tag in {"article", "img"} for tag, _ in journal.tags)


def test_gallery_deduplicates_image_across_tasks_without_mutating_rows(tmp_path):
    rows = [record(1, task_id=1, sha256="same"), record(2, task_id=2, sha256="same", needs_review=True)]
    original = json.loads(json.dumps(rows))
    index = gallery(tmp_path, rows)
    assert seqs(index) == [1]
    assert seqs(index.parent / "needs_review.html") == []
    assert "duplicate_image" in (index.parent / "journal.html").read_text()
    assert rows == original
    assert [row["seq"] for row in map(json.loads, export(tmp_path, rows).read_text().splitlines())] == [1, 2]


def test_excluding_first_copy_keeps_other_selected_copy_visible(tmp_path):
    rows = [record(1, sha256="same", excluded=True), record(2, sha256="same")]
    index = gallery(tmp_path, rows)
    assert seqs(index) == [2]
    assert seqs(index.parent / "excluded.html") == [1]


def test_missing_thumbnail_falls_back_to_cover_and_missing_file_is_not_linked(tmp_path):
    write_image(tmp_path, "covers/1.jpg")
    index = gallery(tmp_path, [record(1), record(2)])
    parsed = Elements(index.read_text())
    assert [attrs["src"] for tag, attrs in parsed.tags if tag == "img"] == ["../covers/1.jpg"]
    assert "../covers/2.jpg" not in index.read_text()
    assert "../thumbnails/" not in index.read_text()


def test_journal_escapes_untrusted_metadata_and_paginates(tmp_path):
    attack = '<script>alert("x")</script>'
    rows = [record(1, status=attack, artist=attack, title=attack, last_error=attack, source_url="javascript:alert(1)"), record(2, status="pending")]
    index = gallery(tmp_path, rows, page_size=1)
    content = (index.parent / "journal.html").read_text()
    assert "&lt;script&gt;" in content
    assert not any(tag == "script" for tag, _ in Elements(content).tags)
    assert "javascript:" not in content
    assert 'href="journal-0002.html" rel="next"' in content
    gallery(tmp_path, [])
    assert not (index.parent / "journal-0002.html").exists()


def test_gallery_rejects_symlink_outside_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "outside").symlink_to(tmp_path, target_is_directory=True)
    index = gallery(data_dir, [record(1, path="outside/secret.jpg", thumbnail_path=None)])
    assert "outside/secret.jpg" not in index.read_text()


@pytest.mark.parametrize("url", ["file:///tmp/a", "//example.com/a", "https://", "https://user:password@example.org/", "https://example.org\\evil", "https://example.org/\n"])
def test_gallery_rejects_non_http_or_malformed_source_urls(tmp_path, url):
    index = gallery(tmp_path, [record(1, source_url=url, path=None, thumbnail_path=None)])
    assert ">Источник</a>" not in index.read_text()


def test_regeneration_removes_stale_pages_but_preserves_other_files(tmp_path):
    index = gallery(tmp_path, [record(1), record(2)], page_size=1)
    note = index.parent / "my-notes.html"
    note.write_text("keep")
    gallery(tmp_path, [])
    assert not (index.parent / "page-0002.html").exists()
    assert note.read_text() == "keep"
    assert "пока нет записей" in index.read_text()
    assert (index.parent / "needs_review.html").exists()
    assert (index.parent / "excluded.html").exists()


@pytest.mark.parametrize("page_size", [0, -1, 1.5, True])
def test_gallery_validates_page_size(tmp_path, page_size):
    with pytest.raises(ValueError):
        gallery(tmp_path, [], page_size=page_size)

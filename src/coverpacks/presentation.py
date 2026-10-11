"""Portable, local manifest and HTML gallery; no network or server required."""

from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlsplit


def _ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (row.get("seq") is None, row.get("seq") or 0))


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export(data_dir: Path, rows: list[dict[str, Any]]) -> Path:
    """Export every field, including nulls, ordered by stable discovery sequence."""
    path = Path(data_dir) / "exports" / "manifest.jsonl"
    # Serialize before touching an existing export so invalid data cannot replace it.
    content = "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        for row in _ordered(rows)
    )
    _write_atomic(path, content)
    return path


def _escape(value: Any, fallback: str = "—") -> str:
    return html.escape(fallback if value is None else str(value), quote=True)


def _external_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    # Browsers may treat backslashes as separators even when urllib does not.
    if "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
    except ValueError:
        return None
    return html.escape(value, quote=True)


def _local_url(data_dir: Path, value: Any) -> str | None:
    """Build a gallery-relative URL for an existing file inside data_dir."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    root = data_dir.resolve()
    try:
        resolved = (root / relative).resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
    except (ValueError, OSError, RuntimeError):
        return None
    if resolved == root:
        return None
    return html.escape(quote("../" + relative.as_posix(), safe="/"), quote=True)


_STYLE = """
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  color: #252920; background: #f2f1eb; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1500px; padding: 32px clamp(16px, 3vw, 48px); }
header { border-bottom: 1px solid #cdd0c4; padding-bottom: 22px; margin-bottom: 24px; }
.eyebrow { text-transform: uppercase; letter-spacing: .14em; color: #596546; font-size: 12px; }
h1 { font-weight: 650; font-size: clamp(30px, 4vw, 48px); margin: 12px 0; letter-spacing: -.04em; }
.intro { color: #596052; max-width: 760px; line-height: 1.55; margin: 0 0 18px; }
a { color: #324d25; text-underline-offset: 3px; }
a:focus-visible { outline: 3px solid #8b9f66; outline-offset: 4px; }
.filters, .pagination { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.filters a, .pagination a, .pagination strong { display: inline-block; padding: 9px 13px;
  border: 1px solid #bec6b5; border-radius: 6px; text-decoration: none; font-size: 14px; }
.filters a[aria-current], .pagination strong { background: #324d25; color: #fff; border-color: #324d25; }
.summary { color: #606754; font-size: 14px; margin: 20px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr)); gap: 20px; }
.card { min-width: 0; background: #fffefa; border: 1px solid #d5d8cd; border-radius: 10px; overflow: hidden; }
.card.excluded { border-style: dashed; }
.art { aspect-ratio: 1; background: #e6e8e0; display: flex; align-items: center; justify-content: center; }
.art img { display: block; width: 100%; height: 100%; object-fit: contain; }
.placeholder { color: #66705b; padding: 28px; text-align: center; }
.details { padding: 18px; overflow-wrap: anywhere; }
.seq { font-size: 12px; font-variant-numeric: tabular-nums; color: #637052; }
h2 { margin: 8px 0 6px; font-size: 18px; line-height: 1.3; }
.artist { margin: 0 0 12px; color: #536045; line-height: 1.4; }
.metadata { font-size: 13px; line-height: 1.6; color: #5c6255; margin: 10px 0; }
.badges { display: flex; flex-wrap: wrap; gap: 5px; }
.badge { display: inline-block; padding: 4px 7px; border-radius: 4px; background: #e9ecdf; font-size: 11px; }
.review { background: #fff0ca; color: #795107; }
.omitted { background: #eeebe4; color: #6a6253; }
.reasons, .error { font-size: 12px; line-height: 1.5; margin: 10px 0; }
.error { color: #8b3f32; }
.links { display: flex; gap: 12px; flex-wrap: wrap; font-size: 13px; margin-top: 14px; }
.pagination { margin: 24px 0; }
.empty { padding: 48px 16px; color: #596052; border: 1px dashed #bec6b5; border-radius: 10px; }
.journal { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; line-height: 1.5; }
th, td { text-align: left; vertical-align: top; padding: 12px; border-bottom: 1px solid #cdd0c4; }
td { overflow-wrap: anywhere; }
footer { margin-top: 30px; font-size: 12px; color: #66705b; line-height: 1.6; }
"""


def _filename(section: str, number: int) -> str:
    if section == "all":
        return "index.html" if number == 1 else f"page-{number:04d}.html"
    return f"{section}.html" if number == 1 else f"{section}-{number:04d}.html"


def _pagination(section: str, number: int, total: int) -> str:
    if total <= 1:
        return ""
    # Bounded navigation keeps every large-collection page small.
    numbers = sorted({1, total, *range(max(1, number - 2), min(total, number + 2) + 1)})
    links: list[str] = []
    if number > 1:
        links.append(f'<a href="{_filename(section, number - 1)}" rel="prev">← Назад</a>')
    previous = 0
    for item in numbers:
        if previous and item > previous + 1:
            links.append('<span aria-hidden="true">…</span>')
        if item == number:
            links.append(f'<strong aria-current="page">{item}</strong>')
        else:
            links.append(f'<a href="{_filename(section, item)}">{item}</a>')
        previous = item
    if number < total:
        links.append(f'<a href="{_filename(section, number + 1)}" rel="next">Далее →</a>')
    return '<nav class="pagination" aria-label="Страницы">' + "".join(links) + "</nav>"


def _card(data_dir: Path, row: dict[str, Any]) -> str:
    local = _local_url(data_dir, row.get("path"))
    preview = _local_url(data_dir, row.get("thumbnail_path"))
    image = preview or local
    title = _escape(row.get("title"), "Без названия")
    artist = _escape(row.get("artist"), "Исполнитель неизвестен")
    artwork = (
        f'<img src="{image}" alt="{artist} — {title}" loading="lazy" decoding="async">'
        if image
        else '<span class="placeholder">Превью недоступно</span>'
    )
    if local:
        artwork = f'<a class="art" href="{local}" aria-label="Открыть локальный файл">{artwork}</a>'
    else:
        artwork = f'<div class="art">{artwork}</div>'
    source_label = "Bandcamp · vinyl" if row.get("source") == "bandcamp" else "MusicBrainz / CAA"
    badges = [f'<span class="badge">{source_label}</span>']
    if row.get("needs_review"):
        badges.append('<span class="badge review">needs_review · проверить</span>')
    if row.get("excluded"):
        badges.append('<span class="badge omitted">excluded · исключено</span>')
    links: list[str] = []
    source = _external_url(row.get("source_url"))
    if source:
        links.append(f'<a href="{source}" rel="noreferrer noopener">Источник</a>')
    remote_image = _external_url(row.get("image_url"))
    if remote_image and remote_image != source:
        links.append(f'<a href="{remote_image}" rel="noreferrer noopener">Исходное изображение</a>')
    if local:
        links.append(f'<a href="{local}">Локальный файл</a>')
    reasons = row.get("review_reasons") or []
    if not isinstance(reasons, (list, tuple)):
        reasons = [reasons]
    review = '<p class="reasons">' + "; ".join(_escape(item) for item in reasons) + "</p>" if reasons else ""
    error = f'<p class="error">{_escape(row["last_error"])}</p>' if row.get("last_error") else ""
    dimensions = f'{_escape(row.get("width"))} × {_escape(row.get("height"))} px'
    metadata = f'{dimensions} · {_escape(row.get("format"))}'
    if row.get("date"):
        metadata += f' · {_escape(row["date"])}'
    card_class = "card excluded" if row.get("excluded") else "card"
    return (
        f'<article class="{card_class}" data-seq="{_escape(row.get("seq"))}">{artwork}'
        f'<div class="details"><span class="seq">SEQ {_escape(row.get("seq"))}</span>'
        f'<h2>{title}</h2><p class="artist">{artist}</p>'
        f'<div class="badges">{"".join(badges)}</div><p class="metadata">{metadata}</p>'
        f'{review}{error}<div class="links">{"".join(links)}</div></div></article>'
    )


def _journal(rows: list[dict[str, Any]]) -> str:
    items = []
    for row in rows:
        source = _external_url(row.get("source_url"))
        title = _escape(row.get("title"), "Без названия")
        if source:
            title = f'<a href="{source}" rel="noreferrer noopener">{title}</a>'
        note = row.get("last_error") or row.get("selection_reason")
        if row.get("excluded"):
            note = "Исключено" + (f" · {note}" if note else "")
        items.append(
            f'<tr data-seq="{_escape(row.get("seq"))}"><td>{_escape(row.get("seq"))}</td>'
            f'<td>{_escape(row.get("status"))}</td><td>{title}<br>{_escape(row.get("artist"))}</td>'
            f'<td>{_escape(note)}</td></tr>'
        )
    return (
        '<main class="journal"><table><thead><tr><th>SEQ</th><th>Статус</th>'
        '<th>Альбом</th><th>Подробности</th></tr></thead><tbody>'
        + "".join(items) + "</tbody></table></main>"
    )


def gallery(data_dir: Path, rows: list[dict[str, Any]], page_size: int = 100, *, directory: str = "gallery", collections=()) -> Path:
    """Show selected covers separately from excluded images and collection history."""
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise ValueError("page_size должен быть положительным целым числом")
    data_dir = Path(data_dir)
    if not re.fullmatch(r"gallery(?:-task-\d+)?",directory):
        raise ValueError("Некорректное имя папки галереи")
    destination = data_dir / directory
    collection_nav = "".join(f'<a href="{_escape(href)}">{_escape(label)}</a>' for href,label in collections)
    ordered = _ordered(rows)
    covers = [row for row in ordered if row.get("status") in {"downloaded", "needs_review"}]
    active = [row for row in covers if not row.get("excluded")]
    journal = [row for row in ordered if row.get("status") not in {"downloaded", "needs_review"}]
    # Separate tasks can select the same image. Keep one visible copy without
    # changing their checkpoint, status, or the complete exported manifest.
    unique = []
    seen = set()
    for row in active:
        sha256 = row.get("sha256")
        if sha256 and sha256 in seen:
            journal.append({**row, "status": "duplicate_image", "selection_reason": "Это изображение уже показано в коллекции"})
            continue
        if sha256:
            seen.add(sha256)
        unique.append(row)
    sections = [
        ("all", "Обложки", unique),
        ("needs_review", "На проверку", [row for row in unique if row.get("needs_review")]),
        ("excluded", "Исключённые", [row for row in covers if row.get("excluded")]),
        ("journal", "Журнал сбора", _ordered(journal)),
    ]
    written: set[str] = set()
    for section, label, selected in sections:
        pages = max(1, math.ceil(len(selected) / page_size))
        filters = "".join(
            f'<a href="{_filename(key, 1)}"'
            + (' aria-current="page"' if key == section else "")
            + f'>{_escape(name)} · {len(items)}</a>'
            for key, name, items in sections
        )
        for number in range(1, pages + 1):
            page_rows = selected[(number - 1) * page_size : number * page_size]
            if not page_rows:
                body = '<main class="empty">В этом разделе пока нет записей. <a href="index.html">Открыть все обложки →</a></main>'
            elif section == "journal":
                body = _journal(page_rows)
            else:
                cards = "".join(_card(data_dir, row) for row in page_rows)
                body = f'<main class="grid">{cards}</main>'
            pagination = _pagination(section, number, pages)
            document = (
                '<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta name="referrer" content="no-referrer">'
                f'<title>coverpacks · {_escape(label)} · {number}</title><style>{_STYLE}</style></head><body>'
                '<header><div class="eyebrow">coverpacks / локальная коллекция</div><h1>Обложки альбомов</h1>'
                '<p class="intro">Здесь собраны выбранные обложки без точных дубликатов. '
                'Кандидаты без обложек и история обработки доступны в журнале сбора.</p>'
                f'<nav class="filters" aria-label="Разделы">{filters}</nav>'
                f'<nav class="links" aria-label="Коллекции">{collection_nav}</nav></header>'
                f'<p class="summary">{_escape(label)}: {len(selected)} · Страница {number} из {pages}</p>'
                f'{pagination}{body}{pagination}'
                '<footer>Статическая галерея. После изменений в базе выполните '
                '<code>coverpacks gallery</code> повторно. '
                'Наличие обложки не означает разрешения на распространение.</footer></body></html>\n'
            )
            name = _filename(section, number)
            _write_atomic(destination / name, document)
            written.add(name)
    # Remove only obsolete pages owned by this generator, preserving other files.
    owned = re.compile(r"(?:index|page-\d+|needs_review(?:-\d+)?|excluded(?:-\d+)?|journal(?:-\d+)?)\.html")
    for path in destination.iterdir():
        if owned.fullmatch(path.name) and path.name not in written and path.is_file():
            path.unlink()
    return destination / "index.html"

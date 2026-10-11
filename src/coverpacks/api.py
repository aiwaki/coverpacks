"""MusicBrainz/CAA metadata and deterministic source selection.

HTTP, retries and persistence belong to ``network`` and the collector. No request
is issued on import. The injected network must provide an async ``json`` method.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from .network import FetchError


MB_URL = "https://musicbrainz.org/ws/2"
CAA_URL = "https://coverartarchive.org"
# An application guard, NOT a claim about a permanent public-API hard limit.
SEARCH_WINDOW = 10_000
MAX_FRONT_IMAGES = 3
VINYL_FORMATS = frozenset({
    "Vinyl", '7" Vinyl', '10" Vinyl', '12" Vinyl',
    "Flexi-disc", '7" Flexi-disc',
})
# MusicBrainz's format field ignores case, spaces and separators. An explicit OR
# is essential: the generic Vinyl term does not mean every sized vinyl format.
VINYL_QUERY = (
    "(format:vinyl OR format:7vinyl OR format:10vinyl OR format:12vinyl "
    "OR format:flexidisc OR format:7flexidisc)"
)


class JSONNetwork(Protocol):
    async def json(
        self, url: str, params: dict[str, Any] | None = None,
        musicbrainz: bool = False,
    ) -> dict[str, Any]: ...


class ApiDataError(ValueError):
    """A valid JSON response has an unexpected or unsafe metadata shape."""


class ArtistAmbiguous(ValueError):
    def __init__(self, name: str, options: list[dict[str, Any]], total: int):
        self.name = name
        self.options = options
        self.total = total
        super().__init__(
            f"Исполнитель {name!r} не определён однозначно ({total} совпадений). "
            "Укажите MBID исполнителя."
        )


@dataclass(frozen=True)
class SearchPage:
    items: list[dict[str, Any]]
    next_offset: int | None
    total: int
    exhausted_reason: str | None = None


@dataclass(frozen=True)
class CoverOption:
    image_id: str
    url: str
    variant: str
    front: bool
    approved: bool | None
    release_id: str | None
    reason: str
    image_url: str | None
    metadata_url: str
    requested_quality: str


def mbid(value: str) -> str:
    """Validate a canonical hyphenated MusicBrainz UUID before URL interpolation."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError(f"Некорректный MusicBrainz MBID: {value!r}")
    return str(UUID(value))


def escape_lucene(value: str) -> str:
    """Escape literals independently of the URL encoding performed by httpx."""
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r"\\\1", value)


def is_vinyl_release(release: dict[str, Any]) -> bool:
    """Accept a release only when an actual medium has a documented vinyl format.

    A bundle with a vinyl medium is a vinyl edition too. Unknown media, shellac,
    acetate, generic phonograph records and VinylDisc are not inferred as vinyl.
    """
    media = release.get("media")
    return isinstance(media, list) and any(
        isinstance(item, dict) and isinstance(item.get("format"), str)
        and item["format"] in VINYL_FORMATS
        for item in media
    )


def is_album_group(group: dict[str, Any]) -> bool:
    return str(group.get("primary-type", "")).casefold() == "album"


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    items = data.get(key)
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        raise ApiDataError(f"В JSON отсутствует корректный массив {key}")
    return items


def _entity(data: dict[str, Any], kind: str) -> dict[str, Any]:
    """Reject incomplete entity IDs before they reach queues or URL builders."""
    try:
        canonical_id = mbid(data.get("id"))
    except ValueError as exc:
        raise ApiDataError(f"{kind} не содержит корректный MBID") from exc
    result = {**data, "id": canonical_id}
    if "release-group" in result:
        group = result["release-group"]
        if not isinstance(group, dict):
            raise ApiDataError("Release содержит некорректный release-group")
        result["release-group"] = _entity(group, "release-group")
    return result


def _number(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiDataError(f"Некорректное поле пагинации {key}: {value!r}")
    return value


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return None


def _release_from_url(value: Any) -> str | None:
    url = _web_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in {"musicbrainz.org", "www.musicbrainz.org", "coverartarchive.org"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "release":
        try:
            return mbid(parts[1])
        except ValueError:
            pass
    return None


def _release_order(release: dict[str, Any]) -> tuple[bool, str, str]:
    date = release.get("date") or ""
    return (not bool(date), str(date), str(release.get("id") or ""))


class CatalogAPI:
    def __init__(self, network: JSONNetwork, search_window: int = SEARCH_WINDOW):
        if search_window < 1:
            raise ValueError("search_window должен быть положительным")
        self.network = network
        self.search_window = search_window

    async def _search(
        self, kind: str, query: str, offset: int, limit: int,
    ) -> SearchPage:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("offset >= 0; 1 <= limit <= 100")
        if offset >= self.search_window:
            return SearchPage([], None, offset, "search_window")
        data = await self.network.json(
            f"{MB_URL}/{kind}",
            params={"query": query, "offset": offset,
                    "limit": min(limit, self.search_window - offset), "fmt": "json"},
            musicbrainz=True,
        )
        items = _items(data, {"release": "releases", "release-group": "release-groups",
                              "artist": "artists"}[kind])
        items = [_entity(item, kind) for item in items]
        total = _number(data, "count")
        returned_offset = _number(data, "offset")
        if returned_offset != offset:
            raise ApiDataError("MusicBrainz вернул неожиданное смещение страницы")
        next_offset = returned_offset + len(items)
        if not items:
            reason = "empty_search_page" if offset < total else "search_results_exhausted"
            return SearchPage(items, None, total, reason)
        if next_offset >= total:
            return SearchPage(items, None, total, "search_results_exhausted")
        if next_offset >= self.search_window:
            return SearchPage(items, None, total, "search_window")
        return SearchPage(items, next_offset, total)

    async def search_page(
        self, mode: str, artist_id: str | None = None,
        offset: int = 0, limit: int = 100,
    ) -> SearchPage:
        if mode not in {"vinyl", "albums"}:
            raise ValueError("mode должен быть vinyl или albums")
        query = "primarytype:album"
        if mode == "vinyl":
            query += f" AND {VINYL_QUERY}"
        if artist_id:
            query += f" AND arid:{mbid(artist_id)}"
        return await self._search(
            "release" if mode == "vinyl" else "release-group", query, offset, limit,
        )

    async def release(self, release_id: str) -> dict[str, Any]:
        data = await self.network.json(
            f"{MB_URL}/release/{mbid(release_id)}",
            params={"inc": "release-groups+artist-credits+media", "fmt": "json"},
            musicbrainz=True,
        )
        if not isinstance(data.get("release-group"), dict):
            raise ApiDataError("Release не содержит id или release-group")
        return _entity(data, "release")

    async def group(self, group_id: str) -> dict[str, Any]:
        # Do not use inc=releases: linked entities in a lookup are capped at 25.
        data = await self.network.json(
            f"{MB_URL}/release-group/{mbid(group_id)}",
            params={"inc": "artist-credits", "fmt": "json"}, musicbrainz=True,
        )
        return _entity(data, "release-group")

    async def releases(
        self, group_id: str, cap: int = 20, vinyl: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a bounded, deterministic set, counting raw scanned editions.

        Album fallback uses browse pagination. Vinyl fallback searches the group
        for all known vinyl formats, then verifies each concrete release lookup.
        A stale search hit consumes cap even when it turns out not to be vinyl.
        """
        group_id = mbid(group_id)
        if cap < 1:
            raise ValueError("cap должен быть положительным")
        offset = 0
        scanned = 0
        found: dict[str, dict[str, Any]] = {}
        while scanned < cap:
            limit = min(100, cap - scanned)
            if vinyl:
                page = await self._search(
                    "release", f"rgid:{group_id} AND {VINYL_QUERY}", offset, limit,
                )
                items, next_offset = page.items, page.next_offset
            else:
                data = await self.network.json(
                    f"{MB_URL}/release",
                    params={"release-group": group_id, "inc": "media+artist-credits+release-groups",
                            "limit": limit, "offset": offset, "fmt": "json"},
                    musicbrainz=True,
                )
                items = [_entity(item, "release") for item in _items(data, "releases")]
                total = _number(data, "release-count")
                returned_offset = _number(data, "release-offset")
                if returned_offset != offset:
                    raise ApiDataError("MusicBrainz browse вернул неожиданное смещение")
                end = offset + len(items)
                next_offset = end if items and end < total else None
            for item in items[:cap - scanned]:
                scanned += 1
                release_id = item["id"]
                if vinyl:
                    try:
                        item = await self.release(release_id)
                    except FetchError as exc:
                        if exc.status == "not_found":
                            # A removed/merged stale search hit consumes the
                            # candidate budget but must not discard prior hits.
                            continue
                        raise
                    if not is_vinyl_release(item):
                        continue
                candidate_group = item.get("release-group")
                if isinstance(candidate_group, dict) and candidate_group.get("id") != group_id:
                    continue
                found[item["id"]] = item
            if next_offset is None:
                break
            offset = next_offset
        return sorted(found.values(), key=_release_order)

    async def resolve_artist(self, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("Имя исполнителя не должно быть пустым")
        page = await self._search("artist", f'artist:"{escape_lucene(name)}"', 0, 100)
        if not page.items:
            raise ValueError(f"Исполнитель {name!r} не найден. Укажите MBID.")
        # Do not trust a top score as proof of identity, nor choose a homonym
        # based on country/popularity. Additional results can be on later pages.
        only = page.items[0]
        if page.total == 1 and len(page.items) == 1 and str(only.get("name", "")).casefold() == name.casefold():
            return only
        options = sorted(page.items, key=lambda i: (
            str(i.get("name", "")).casefold(), str(i.get("disambiguation", "")), str(i.get("id", "")),
        ))
        raise ArtistAmbiguous(name, options, page.total)

    async def cover_options(
        self, kind: str, entity_id: str, quality: str = "1200",
    ) -> list[CoverOption]:
        """Front images only, in deterministic download-attempt order.

        Missing fronts return []; HTTP 404 and transient errors retain their
        network exception types so the collector can record distinct outcomes.
        """
        if kind not in {"release", "release-group"}:
            raise ValueError("CAA kind должен быть release или release-group")
        if quality not in {"1200", "original"}:
            raise ValueError("quality должен быть 1200 или original")
        entity_id = mbid(entity_id)
        metadata_url = f"{CAA_URL}/{kind}/{entity_id}"
        data = await self.network.json(metadata_url)
        images = _items(data, "images")
        release_id = _release_from_url(data.get("release"))
        if kind == "release":
            if release_id is not None and release_id != entity_id:
                raise ApiDataError("CAA metadata относится к другому release")
            release_id = entity_id
        fronts = [i for i in images if i.get("front") is True]
        for item in fronts:
            if not str(item.get("id", "")).isdigit():
                raise ApiDataError("CAA front не содержит числовой image ID")
        fronts.sort(key=lambda i: (i.get("approved") is not True, int(i["id"])))
        variants = ("1200", "500", "250", "original") if quality == "1200" else ("original", "1200", "500", "250")
        result: list[CoverOption] = []
        for item in fronts[:MAX_FRONT_IMAGES]:
            image_id = str(item["id"])
            image_url = _web_url(item.get("image"))
            source_release = release_id or _release_from_url(image_url)
            thumbnails = item.get("thumbnails")
            if not isinstance(thumbnails, dict):
                thumbnails = {}
            urls = {
                "original": image_url,
                "1200": _web_url(thumbnails.get("1200")),
                "500": _web_url(thumbnails.get("500")) or _web_url(thumbnails.get("large")),
                "250": _web_url(thumbnails.get("250")) or _web_url(thumbnails.get("small")),
            }
            approved = item.get("approved") if isinstance(item.get("approved"), bool) else None
            seen_urls: set[str] = set()
            for variant in variants:
                url = urls[variant]
                if url is None or url in seen_urls:
                    continue
                seen_urls.add(url)
                fallback = "requested" if variant == quality else f"fallback_from_{quality}"
                result.append(CoverOption(
                    image_id=image_id, url=url, variant=variant, front=True,
                    approved=approved, release_id=source_release,
                    reason=f"CAA main front; approved={approved}; {fallback}; version={variant}",
                    image_url=image_url, metadata_url=metadata_url, requested_quality=quality,
                ))
        return result

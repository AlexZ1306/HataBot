from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from hata_bot.browser.chrome_cdp import ChromeCdpFetcher
from hata_bot.exceptions import ProviderError, SuspiciousResponseError
from hata_bot.fingerprints import build_content_fingerprint, format_area, normalize_text
from hata_bot.models import Listing, ProviderFetchStats, SourceConfig
from hata_bot.providers.base import ListingProvider, listing_matches_source_filters


class DomclickProvider(ListingProvider):
    CARD_SELECTOR = '[data-e2e-id="offers-list__item"]'
    MIN_EXPECTED_ITEMS = 5
    PAGE_SIZE = 20
    DISTANCE_RE = re.compile(r"~?\s*\d+\s*мин\.?", re.IGNORECASE)
    DISTRICT_SLUGS: list[tuple[str, str]] = [
        ("dzerzhinskij", "Дзержинский"),
        ("oktyabrskij", "Октябрьский"),
        ("zheleznodorozhnyj", "Железнодорожный"),
        ("centralnyj", "Центральный"),
        ("zaelcovskij", "Заельцовский"),
        ("kalininskij", "Калининский"),
        ("kirovskij", "Кировский"),
        ("leninskij", "Ленинский"),
        ("pervomajskij", "Первомайский"),
        ("sovetskij", "Советский"),
    ]

    def __init__(self, source: SourceConfig, *, data_dir: Path) -> None:
        self.source = source
        self.data_dir = data_dir
        self.profile_dir = self.data_dir / "browser_profiles" / source.source_key
        self.last_fetch_stats = ProviderFetchStats(scanned_count=0, matched_count=0, pages_checked=0)

    def fetch(self) -> list[Listing]:
        profile_base_dir = self.data_dir / "browser_profiles"
        profile_base_dir.mkdir(parents=True, exist_ok=True)
        temp_profile_dir = Path(tempfile.mkdtemp(prefix=f"{self.source.source_key}-", dir=profile_base_dir))
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        scanned_count = 0
        pages_checked = 0

        try:
            fetcher = ChromeCdpFetcher(profile_dir=temp_profile_dir)
            for page in range(1, self.source.max_pages + 1):
                offset = (page - 1) * self.PAGE_SIZE
                html = fetcher.fetch_html(
                    url=self._build_search_url(offset=offset),
                    ready_expression="document.querySelectorAll('[data-e2e-id=\"offers-list__item\"]').length > 0",
                    timeout_sec=max(30, self.source.request_timeout_sec),
                )
                page_scanned = self._count_cards(html)
                pages_checked = page

                if page == 1 and page_scanned < self.MIN_EXPECTED_ITEMS:
                    raise SuspiciousResponseError(
                        f"Domclick returned too few cards on page 1: {page_scanned} < {self.MIN_EXPECTED_ITEMS}"
                    )
                if page > 1 and page_scanned == 0:
                    break

                scanned_count += page_scanned
                page_items = self.parse_listings_from_html(source=self.source, html=html, offset=offset)
                for item in page_items:
                    if item.external_id in seen_ids:
                        continue
                    seen_ids.add(item.external_id)
                    listings.append(item)
        finally:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)

        if not listings:
            raise SuspiciousResponseError("Domclick returned no usable listings after parsing.")

        listings.sort(key=self._listing_sort_key, reverse=True)
        self.last_fetch_stats = ProviderFetchStats(
            scanned_count=scanned_count,
            matched_count=len(listings),
            pages_checked=pages_checked,
        )
        return listings

    @classmethod
    def parse_listings_from_html(cls, *, source: SourceConfig, html: str, offset: int = 0) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        state = cls._extract_state(html)
        page_data = cls._extract_page_data(state, offset=offset)
        cards_by_id = cls._map_cards_by_offer_id(soup)
        entities = page_data.get("entities", {})

        listings: list[Listing] = []
        for offer_id in page_data.get("ids", []):
            entity = entities.get(str(offer_id)) or entities.get(offer_id)
            if not isinstance(entity, dict):
                continue

            listing = cls._build_listing(
                source=source,
                entity=entity,
                card=cards_by_id.get(str(offer_id)),
            )
            if listing is not None:
                listings.append(listing)

        return listings

    def _build_search_url(self, *, offset: int = 0) -> str:
        parsed = urlparse(self.source.search_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["offset"] = str(offset)
        if self.source.sort_override:
            query["sort"] = self.source.sort_override
            query.setdefault("sort_dir", "desc")
        return urlunparse(parsed._replace(query=urlencode(query)))

    @classmethod
    def _build_listing(cls, *, source: SourceConfig, entity: dict, card) -> Listing | None:
        external_id = str(entity.get("id") or "").strip()
        if not external_id:
            return None

        object_info = entity.get("objectInfo") or {}
        house = entity.get("house") or {}
        rooms = cls._as_int(object_info.get("rooms"))
        area_m2 = cls._as_float(object_info.get("area"))
        floor = cls._as_int(object_info.get("floor"))
        floors = cls._as_int(house.get("floors"))
        title = cls._compose_title(rooms=rooms, area_m2=area_m2, floor=floor, floors=floors)

        address = normalize_text((entity.get("address") or {}).get("displayName"))
        district = cls._extract_district(entity)
        if source.required_districts and not cls._district_allowed(district, source.required_districts):
            return None

        description = normalize_text(entity.get("description"))
        combined_text = " ".join(filter(None, [title, address, district, description]))
        if cls._matches_excluded_text(combined_text, source.exclude_text_patterns):
            return None

        price_rub = cls._as_int(entity.get("price"))
        metro = cls._extract_metro_from_card(card, address=address)
        photo_urls = cls._extract_photo_urls(entity)
        image_url = photo_urls[0] if photo_urls else None
        path = entity.get("path") or ""
        if not isinstance(path, str) or not path.startswith("http"):
            return None

        updated_iso = cls._normalize_iso(entity.get("updatedDate"))
        published_iso = cls._normalize_iso(entity.get("publishedDate"))
        published_text = cls._extract_published_text_from_card(card) or cls._humanize_timestamp(updated_iso or published_iso)

        fingerprint = build_content_fingerprint(
            source_key=source.source_key,
            title=title,
            price_rub=price_rub,
            rooms=rooms,
            area_m2=area_m2,
            address=address or district or title,
        )

        raw_payload = {
            "district": district,
            "description": description,
            "published_iso": published_iso,
            "updated_iso": updated_iso,
            "image_url": image_url,
            "photo_urls": photo_urls,
        }
        if metro:
            raw_payload["metro"] = metro

        listing = Listing(
            source_key=source.source_key,
            external_id=external_id,
            url=path,
            title=title,
            price_rub=price_rub,
            rooms=rooms,
            area_m2=area_m2,
            address=address or None,
            metro=metro,
            published_text=published_text,
            content_fingerprint=fingerprint,
            image_url=image_url,
            photo_urls=photo_urls,
            raw_payload=raw_payload,
        )

        if not listing_matches_source_filters(listing, source):
            return None
        return listing

    @classmethod
    def _extract_state(cls, html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if text and "window.__SSR_STATE__=" in text:
                payload = cls._extract_js_object(text, marker="window.__SSR_STATE__=")
                payload = re.sub(r"\bundefined\b", "null", payload)
                return json.loads(payload)

        raise ProviderError("Domclick page does not contain __SSR_STATE__.")

    @staticmethod
    def _extract_js_object(script_text: str, *, marker: str) -> str:
        marker_index = script_text.find(marker)
        if marker_index < 0:
            raise ProviderError(f"Domclick page does not contain marker {marker!r}.")

        start = script_text.find("{", marker_index + len(marker))
        if start < 0:
            raise ProviderError(f"Domclick page contains malformed payload for {marker!r}.")

        depth = 0
        in_string = False
        string_quote = ""
        escaped = False

        for index in range(start, len(script_text)):
            char = script_text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == string_quote:
                    in_string = False
                continue

            if char in {'"', "'"}:
                in_string = True
                string_quote = char
                continue

            if char == "{":
                depth += 1
                continue

            if char == "}":
                depth -= 1
                if depth == 0:
                    return script_text[start : index + 1]

        raise ProviderError(f"Domclick page contains unterminated payload for {marker!r}.")

    @staticmethod
    def _extract_page_data(state: dict, *, offset: int) -> dict:
        pages = (((state.get("search") or {}).get("pages")) or {})
        page_key = str(offset)
        if page_key in pages:
            return pages[page_key]
        if len(pages) == 1:
            return next(iter(pages.values()))
        raise ProviderError(f"Domclick state does not contain page data for offset={offset}.")

    @classmethod
    def _map_cards_by_offer_id(cls, soup: BeautifulSoup) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for card in soup.select(cls.CARD_SELECTOR):
            link = card.select_one('a[href*="/card/rent__flat__"]')
            href = link.get("href") if link is not None else None
            offer_id = cls._extract_offer_id(href)
            if offer_id:
                mapping[offer_id] = card
        return mapping

    @staticmethod
    def _extract_offer_id(url: str | None) -> str | None:
        if not url:
            return None
        match = re.search(r"rent__flat__(\d+)", url)
        if not match:
            return None
        return match.group(1)

    @classmethod
    def _extract_district(cls, entity: dict) -> str | None:
        parts = ((entity.get("seoInfo") or {}).get("displayNameParts")) or []
        uris = [part.get("uri") for part in parts if isinstance(part, dict) and part.get("uri")]

        for slug, name in cls.DISTRICT_SLUGS:
            for uri in uris:
                if slug in uri:
                    return name

        description = normalize_text(entity.get("description"))
        for _, name in cls.DISTRICT_SLUGS:
            if name.casefold() in description.casefold():
                return name
        return None

    @classmethod
    def _extract_metro_from_card(cls, card, *, address: str) -> str | None:
        if card is None or not address:
            return None

        lines = [normalize_text(item) for item in card.stripped_strings if normalize_text(item)]
        for index, line in enumerate(lines):
            if line == address:
                if index + 2 < len(lines) and cls.DISTANCE_RE.fullmatch(lines[index + 2]):
                    return f"{lines[index + 1]}, {lines[index + 2]}"
                if index + 1 < len(lines) and not cls.DISTANCE_RE.fullmatch(lines[index + 1]):
                    next_line = lines[index + 1]
                    if next_line not in {"Показать телефон", "Написать"} and not re.search(r"\d+-комн\. квартира", next_line):
                        return next_line
                continue

            if not line.startswith(address):
                continue

            tail = normalize_text(line[len(address) :])
            if not tail:
                continue
            distance_match = cls.DISTANCE_RE.search(tail)
            if distance_match:
                station = normalize_text(tail[: distance_match.start()])
                distance = normalize_text(distance_match.group(0))
                if station:
                    return f"{station}, {distance}"
            if tail not in {"Показать телефон", "Написать"}:
                return tail
        return None

    @staticmethod
    def _extract_published_text_from_card(card) -> str | None:
        if card is None:
            return None
        node = card.select_one('[data-test="updated-date"]')
        if node is None:
            return None
        return normalize_text(node.get_text(" ", strip=True))

    @staticmethod
    def _extract_photo_urls(entity: dict) -> list[str]:
        urls: list[str] = []
        for photo in entity.get("photos") or []:
            raw_url = photo.get("url") if isinstance(photo, dict) else None
            if not raw_url:
                continue
            if raw_url.startswith("http"):
                urls.append(raw_url)
            elif raw_url.startswith("/"):
                urls.append(f"https://img.dmclk.ru/c960x640q80{raw_url}")

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    @staticmethod
    def _compose_title(*, rooms: int | None, area_m2: float | None, floor: int | None, floors: int | None) -> str:
        parts: list[str] = []
        if rooms is not None:
            parts.append(f"{rooms}-комн. квартира")
        else:
            parts.append("Квартира")
        if area_m2 is not None:
            parts.append(f"{format_area(area_m2)} м²")
        if floor is not None and floors is not None:
            parts.append(f"{floor}/{floors} эт.")
        elif floor is not None:
            parts.append(f"{floor} эт.")
        return ", ".join(parts)

    @staticmethod
    def _matches_excluded_text(text: str, patterns: list[str]) -> bool:
        lowered = text.casefold()
        return any(pattern.casefold() in lowered for pattern in patterns)

    @staticmethod
    def _district_allowed(district: str | None, allowed_districts: list[str]) -> bool:
        if not district:
            return False
        normalized = district.casefold()
        return any(item.casefold() == normalized for item in allowed_districts)

    @classmethod
    def _listing_sort_key(cls, listing: Listing):
        raw_payload = listing.raw_payload if isinstance(listing.raw_payload, dict) else {}
        timestamp = raw_payload.get("published_iso") or raw_payload.get("updated_iso")
        return cls._parse_iso(timestamp) or datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _count_cards(html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        return len(soup.select(DomclickProvider.CARD_SELECTOR))

    @staticmethod
    def _normalize_iso(value) -> str | None:
        if not isinstance(value, str):
            return None
        return value.strip() or None

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @classmethod
    def _humanize_timestamp(cls, value: str | None) -> str | None:
        dt = cls._parse_iso(value)
        if dt is None:
            return None

        now = datetime.now(timezone.utc)
        delta = now - dt.astimezone(timezone.utc)
        if delta.total_seconds() < 0:
            return "обновлено недавно"

        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"обновлено {max(1, minutes)} мин. назад"

        hours = minutes // 60
        if hours < 24:
            return f"обновлено {hours} ч. назад"

        days = delta.days
        if days < 7:
            return f"обновлено {days} дн. назад"

        return "обновлено " + dt.astimezone(timezone.utc).strftime("%d.%m.%Y")

    @staticmethod
    def _as_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

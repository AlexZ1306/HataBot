from __future__ import annotations

import base64
import json
import re
from typing import Any
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from hata_bot.exceptions import ProviderError, SuspiciousResponseError
from hata_bot.fingerprints import build_content_fingerprint, normalize_text, parse_rooms_and_area
from hata_bot.http import build_session
from hata_bot.models import Listing, ProviderFetchStats, SourceConfig
from hata_bot.providers.base import ListingProvider, listing_matches_source_filters
from hata_bot.search_profile import AVITO_DISTRICT_IDS


class AvitoProvider(ListingProvider):
    MIN_EXPECTED_ITEMS = 5
    STRONG_BLOCK_MARKERS = (
        "Подтвердите, что вы не робот",
        "доступ ограничен",
        "access to resource was blocked",
        "unusual traffic",
    )
    WEAK_BLOCK_MARKERS = (
        "captcha",
        "recaptcha",
    )

    def __init__(self, source: SourceConfig, session: requests.Session | None = None) -> None:
        self.source = source
        self.session = session or build_session(user_agent=source.user_agent)
        self.last_fetch_stats = ProviderFetchStats(scanned_count=0, matched_count=0, pages_checked=0)

    def fetch(self) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        pages_checked = 0

        for page in range(1, self.source.max_pages + 1):
            url = self._build_page_url(page)
            response = self.session.get(url, timeout=self.source.request_timeout_sec)
            if response.status_code >= 400:
                raise ProviderError(f"Avito responded with HTTP {response.status_code} for {url}")

            html = response.text
            self._ensure_not_suspicious(html=html, page=page, url=url)
            page_items = self.parse_listings_from_html(source_key=self.source.source_key, html=html)
            page_items = [item for item in page_items if listing_matches_source_filters(item, self.source)]
            pages_checked = page

            if page == 1 and len(page_items) < self.MIN_EXPECTED_ITEMS:
                raise SuspiciousResponseError(
                    f"Avito returned too few items on page 1: {len(page_items)} < {self.MIN_EXPECTED_ITEMS}"
                )
            if page > 1 and not page_items:
                break

            for item in page_items:
                if item.external_id not in seen_ids:
                    listings.append(item)
                    seen_ids.add(item.external_id)

        if not listings:
            raise SuspiciousResponseError("Avito returned no listings after parsing.")

        self.last_fetch_stats = ProviderFetchStats(
            scanned_count=len(listings),
            matched_count=len(listings),
            pages_checked=pages_checked,
        )
        return listings

    @classmethod
    def parse_listings_from_html(cls, *, source_key: str, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[data-marker="item"][data-item-id]')
        seller_meta_by_id = cls._extract_seller_metadata_map(html)
        listings: list[Listing] = []

        for card in cards:
            external_id = card.get("data-item-id")
            if not external_id:
                continue

            title_node = card.select_one('[data-marker="item-title"]')
            if title_node is None:
                continue

            title = normalize_text(unescape(title_node.get_text(" ", strip=True)))
            href = title_node.get("href")
            if not href:
                continue
            url = href if href.startswith("http") else f"https://www.avito.ru{href}"

            price_node = card.select_one('meta[itemprop="price"]')
            price_rub = int(price_node.get("content")) if price_node and price_node.get("content") else None

            rooms, area_m2 = parse_rooms_and_area(title)

            address_lines = [
                normalize_text(unescape(line.get_text(" ", strip=True)))
                for line in card.select('[data-marker="item-address"] p')
                if normalize_text(line.get_text(" ", strip=True))
            ]
            address = address_lines[0] if address_lines else None
            metro = address_lines[1] if len(address_lines) > 1 else None

            published_node = card.select_one('[data-marker="item-date"]')
            published_text = normalize_text(unescape(published_node.get_text(" ", strip=True))) if published_node else None

            params_node = card.select_one('[data-marker="item-specific-params"]')
            specific_params = normalize_text(unescape(params_node.get_text(" ", strip=True))) if params_node else None
            photo_urls = cls._extract_photo_urls(card)
            image_url = photo_urls[0] if photo_urls else None
            seller_meta = seller_meta_by_id.get(external_id, {})
            seller_label = normalize_text(str(seller_meta.get("seller_label") or "")) or None
            seller_name = normalize_text(str(seller_meta.get("seller_name") or "")) or cls._extract_seller_name_from_card(card)
            seller_kind = cls._classify_seller_kind(seller_label=seller_label, card=card)

            fingerprint = build_content_fingerprint(
                source_key=source_key,
                title=title,
                price_rub=price_rub,
                rooms=rooms,
                area_m2=area_m2,
                address=address,
            )

            listings.append(
                Listing(
                    source_key=source_key,
                    external_id=external_id,
                    url=url,
                    title=title,
                    price_rub=price_rub,
                    rooms=rooms,
                    area_m2=area_m2,
                    address=address,
                    metro=metro,
                    published_text=published_text,
                    content_fingerprint=fingerprint,
                    seller_kind=seller_kind,
                    seller_name=seller_name,
                    image_url=image_url,
                    photo_urls=photo_urls,
                    raw_payload={
                        "specific_params": specific_params,
                        "published_text": published_text,
                        "address_lines": address_lines,
                        "seller_label": seller_label,
                        "image_url": image_url,
                        "photo_urls": photo_urls,
                    },
                )
            )

        return listings

    @staticmethod
    def _extract_photo_urls(card) -> list[str]:
        urls: list[str] = []

        for img in card.select("img"):
            src = img.get("src")
            if src and src.startswith("https://"):
                urls.append(src)

        for node in card.select("[data-marker]"):
            marker = node.get("data-marker")
            if not marker or "slider-image/image-" not in marker:
                continue
            prefix = "slider-image/image-"
            image_url = marker.split(prefix, 1)[1]
            if image_url.startswith("https://"):
                urls.append(image_url)

        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    @classmethod
    def _extract_seller_metadata_map(cls, html: str) -> dict[str, dict[str, str | None]]:
        metadata: dict[str, dict[str, str | None]] = {}
        cursor = 0
        id_start_pattern = re.compile(r'\{\s*"id"\s*:')

        while True:
            match = id_start_pattern.search(html, cursor)
            if match is None:
                break
            start = match.start()

            fragment = cls._extract_json_object_fragment(html, start)
            if fragment is None:
                cursor = start + 5
                continue

            object_text, end = fragment
            cursor = end

            try:
                payload = json.loads(object_text)
            except json.JSONDecodeError:
                continue

            if not isinstance(payload, dict):
                continue
            if "iva" not in payload or "urlPath" not in payload:
                continue

            external_id = str(payload.get("id") or "").strip()
            iva = payload.get("iva")
            if not external_id or not isinstance(iva, dict):
                continue

            seller_label = cls._extract_iva_step_label(iva.get("SecondLineStep"))
            seller_name = cls._extract_iva_seller_name(iva.get("UserInfoStep"))
            if not seller_label and not seller_name:
                continue

            metadata[external_id] = {
                "seller_label": seller_label,
                "seller_name": seller_name,
            }

        return metadata

    @staticmethod
    def _extract_json_object_fragment(payload: str, start: int) -> tuple[str, int] | None:
        if start < 0 or start >= len(payload) or payload[start] != "{":
            return None

        depth = 0
        in_string = False
        escaped = False

        for index in range(start, len(payload)):
            char = payload[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue

            if char == "{":
                depth += 1
                continue

            if char == "}":
                depth -= 1
                if depth == 0:
                    return payload[start : index + 1], index + 1

        return None

    @staticmethod
    def _extract_iva_step_label(steps: Any) -> str | None:
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict):
                continue
            payload = step.get("payload")
            if not isinstance(payload, dict):
                continue
            value = normalize_text(str(payload.get("value") or ""))
            if value:
                return value
        return None

    @staticmethod
    def _extract_iva_seller_name(steps: Any) -> str | None:
        if not isinstance(steps, list):
            return None
        for step in steps:
            if not isinstance(step, dict):
                continue
            payload = step.get("payload")
            if not isinstance(payload, dict):
                continue
            profile = payload.get("profile")
            if not isinstance(profile, dict):
                continue
            title = normalize_text(str(profile.get("title") or ""))
            if title:
                return title
        return None

    @staticmethod
    def _extract_seller_name_from_card(card) -> str | None:
        seller_link = card.select_one('a[href*="search_seller_info"], a[href*="/brands/"]')
        if seller_link is None:
            return None
        return normalize_text(unescape(seller_link.get_text(" ", strip=True))) or None

    @staticmethod
    def _classify_seller_kind(*, seller_label: str | None, card) -> str | None:
        lowered = (seller_label or "").casefold()
        if "собствен" in lowered or "частн" in lowered:
            return "owner"
        if "агент" in lowered:
            return "agency"
        if "компан" in lowered:
            return "company"
        if "риелтор" in lowered:
            return "agent"
        if card.select_one('a[href*="/brands/"]') is not None:
            return "agency"
        return None

    def _ensure_not_suspicious(self, *, html: str, page: int, url: str) -> None:
        body = html.strip()
        if not body:
            raise SuspiciousResponseError(f"Empty response from Avito for {url}")

        lowered = body.lower()
        has_listing_markers = 'data-marker="item"' in body or "data-item-id" in body

        for marker in self.STRONG_BLOCK_MARKERS:
            if marker.lower() in lowered:
                raise SuspiciousResponseError(f"Avito returned a suspicious anti-bot page for {url}")

        if not has_listing_markers and any(marker in lowered for marker in self.WEAK_BLOCK_MARKERS):
            raise SuspiciousResponseError(f"Avito returned a suspicious anti-bot page for {url}")

        if page == 1 and not has_listing_markers:
            raise SuspiciousResponseError(f"Avito page 1 does not look like a listing page for {url}")

    def _build_page_url(self, page: int) -> str:
        parsed = urlparse(self.source.search_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))

        filter_token = query.get("f")
        if filter_token:
            query["f"] = self._rewrite_filter_token(filter_token)

        district_ids = [AVITO_DISTRICT_IDS[district] for district in self.source.required_districts if district in AVITO_DISTRICT_IDS]
        if district_ids:
            query["district"] = "-".join(district_ids)

        if page > 1:
            query["p"] = str(page)
        else:
            query.pop("p", None)

        return urlunparse(parsed._replace(query=urlencode(query)))

    def _rewrite_filter_token(self, token: str) -> str:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

        min_area = int(self.source.min_area_m2 or 0)
        min_price = int(self.source.min_price_rub or 0)
        max_price = int(self.source.max_price_rub) if self.source.max_price_rub is not None else None

        area_payload = json.dumps({"from": min_area, "to": None}, separators=(",", ":")).encode("utf-8")
        price_payload = json.dumps({"from": min_price, "to": max_price}, separators=(",", ":")).encode("utf-8")

        pattern = rb'\{"from":\d+,"to":(?:null|\d+)\}'
        matches = list(re.finditer(pattern, raw))
        if len(matches) >= 2:
            raw = self._replace_match(raw, matches[1], price_payload)
        if len(matches) >= 1:
            raw = self._replace_match(raw, matches[0], area_payload)
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _replace_match(payload: bytes, match: re.Match[bytes], replacement: bytes) -> bytes:
        return payload[: match.start()] + replacement + payload[match.end() :]

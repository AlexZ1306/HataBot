from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from hata_bot.browser.chrome_cdp import ChromeCdpFetcher
from hata_bot.exceptions import SuspiciousResponseError
from hata_bot.fingerprints import build_content_fingerprint, normalize_text, parse_rooms_and_area
from hata_bot.models import Listing, ProviderFetchStats, SourceConfig
from hata_bot.providers.base import ListingProvider, listing_matches_source_filters


class CianProvider(ListingProvider):
    MIN_EXPECTED_ITEMS = 3

    def __init__(self, source: SourceConfig, *, data_dir: Path) -> None:
        self.source = source
        self.data_dir = data_dir
        self.profile_dir = self.data_dir / "browser_profiles" / source.source_key
        self.last_fetch_stats = ProviderFetchStats(scanned_count=0, matched_count=0, pages_checked=0)

    def fetch(self) -> list[Listing]:
        fetcher = ChromeCdpFetcher(profile_dir=self.profile_dir)
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        scanned_count = 0
        pages_checked = 0

        for page in range(1, self.source.max_pages + 1):
            html = fetcher.fetch_html(
                url=self._build_search_url(page=page),
                ready_expression="document.querySelectorAll('[data-name=\"CardComponent\"]').length > 0",
                timeout_sec=max(25, self.source.request_timeout_sec),
            )
            page_scanned = self._count_cards(html)
            pages_checked = page

            if page == 1 and page_scanned < self.MIN_EXPECTED_ITEMS:
                raise SuspiciousResponseError(
                    f"Cian returned too few cards on page 1: {page_scanned} < {self.MIN_EXPECTED_ITEMS}"
                )
            if page > 1 and page_scanned == 0:
                break

            scanned_count += page_scanned
            page_items = self.parse_listings_from_html(source=self.source, html=html)
            for item in page_items:
                if item.external_id in seen_ids:
                    continue
                seen_ids.add(item.external_id)
                listings.append(item)

        if not listings:
            raise SuspiciousResponseError("Cian returned no usable listings after parsing.")
        if len(listings) < self.MIN_EXPECTED_ITEMS:
            raise SuspiciousResponseError(
                f"Cian returned too few usable items: {len(listings)} < {self.MIN_EXPECTED_ITEMS}"
            )

        self.last_fetch_stats = ProviderFetchStats(
            scanned_count=scanned_count,
            matched_count=len(listings),
            pages_checked=pages_checked,
        )
        return listings

    @classmethod
    def parse_listings_from_html(cls, *, source: SourceConfig, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[data-name="CardComponent"]')
        listings: list[Listing] = []

        for card in cards:
            title_node = card.select_one('[data-name="TitleComponent"][href]')
            if title_node is None:
                continue

            url = title_node.get("href")
            if not url:
                continue
            external_id = cls._extract_offer_id(url)
            if external_id is None:
                continue

            title = normalize_text(unescape(title_node.get_text(" ", strip=True)))
            rooms, area_m2 = parse_rooms_and_area(title)

            details_text = cls._extract_details_text(card)
            combined_text = " ".join(
                filter(
                    None,
                    [
                        title,
                        details_text,
                        normalize_text(unescape(card.get_text(" ", strip=True))),
                    ],
                )
            )
            if cls._matches_excluded_text(combined_text, source.exclude_text_patterns):
                continue

            geo_labels = [normalize_text(unescape(node.get_text(" ", strip=True))) for node in card.select('[data-name="GeoLabel"]')]
            district = cls._extract_district(geo_labels)
            if source.required_districts and not cls._district_allowed(district, source.required_districts):
                continue

            address = cls._build_address(geo_labels)
            metro = cls._extract_metro(card)
            price_rub = cls._extract_price_rub(details_text)
            published_text = cls._extract_published_text(card)
            photo_urls = cls._extract_photo_urls(card)
            image_url = photo_urls[0] if photo_urls else None

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
                "details_text": details_text,
                "geo_labels": geo_labels,
                "image_url": image_url,
                "photo_urls": photo_urls,
            }
            if metro:
                raw_payload["metro"] = metro

            listings.append(
                Listing(
                    source_key=source.source_key,
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
                    image_url=image_url,
                    photo_urls=photo_urls,
                    raw_payload=raw_payload,
                )
            )

        return [item for item in listings if listing_matches_source_filters(item, source)]

    def _build_search_url(self, *, page: int = 1) -> str:
        parsed = urlparse(self.source.search_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if self.source.sort_override:
            query["sort"] = self.source.sort_override
        elif "sort" not in query:
            query["sort"] = "creation_date_desc"
        if page > 1:
            query["p"] = str(page)
        else:
            query.pop("p", None)
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _count_cards(html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        return len(soup.select('[data-name="CardComponent"]'))

    @staticmethod
    def _extract_offer_id(url: str) -> str | None:
        match = re.search(r"/rent/flat/(\d+)/", url)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _extract_price_rub(details_text: str | None) -> int | None:
        if not details_text:
            return None
        match = re.search(r"([\d\s]+)\s*₽/мес", details_text)
        if not match:
            return None
        digits = re.sub(r"[^\d]", "", match.group(1))
        return int(digits) if digits else None

    @staticmethod
    def _extract_district(geo_labels: list[str]) -> str | None:
        for label in geo_labels:
            if label.lower().startswith("р-н "):
                return label[4:].strip()
        return None

    @staticmethod
    def _build_address(geo_labels: list[str]) -> str | None:
        filtered: list[str] = []
        for label in geo_labels:
            lowered = label.lower()
            if label == "Новосибирская область":
                continue
            if label == "Новосибирск":
                continue
            if lowered.startswith("р-н "):
                continue
            if lowered.startswith("м. "):
                continue
            filtered.append(label)
        if not filtered:
            return None
        return ", ".join(filtered)

    @staticmethod
    def _extract_metro(card) -> str | None:
        special_geo = card.select_one('[data-name="SpecialGeo"]')
        if special_geo is None:
            return None
        return normalize_text(unescape(special_geo.get_text(" ", strip=True)))

    @staticmethod
    def _extract_published_text(card) -> str | None:
        node = card.select_one('[data-name="TimeLabel"]')
        if node is None:
            return None
        return normalize_text(unescape(node.get_text(" ", strip=True)))

    @staticmethod
    def _extract_details_text(card) -> str | None:
        rows = [
            normalize_text(unescape(node.get_text(" ", strip=True)))
            for node in card.select('[data-name="GeneralInfoSectionRowComponent"]')
        ]
        for row in rows:
            if "₽/мес" in row:
                return row
        return None

    @staticmethod
    def _extract_photo_urls(card) -> list[str]:
        urls: list[str] = []
        for img in card.select('[data-name="Gallery"] img[src]'):
            src = img.get("src")
            if src and src.startswith("https://images.cdn-cian.ru/"):
                urls.append(src)
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped

    @staticmethod
    def _matches_excluded_text(text: str, patterns: list[str]) -> bool:
        lowered = text.casefold()
        return any(pattern.casefold() in lowered for pattern in patterns)

    @staticmethod
    def _district_allowed(district: str | None, allowed_districts: list[str]) -> bool:
        if not district:
            return False
        normalized_district = district.casefold()
        return any(item.casefold() == normalized_district for item in allowed_districts)

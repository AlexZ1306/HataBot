from __future__ import annotations

from html import unescape
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from hata_bot.exceptions import ProviderError, SuspiciousResponseError
from hata_bot.fingerprints import build_content_fingerprint, normalize_text, parse_rooms_and_area
from hata_bot.http import build_session
from hata_bot.models import Listing, SourceConfig
from hata_bot.providers.base import ListingProvider


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

    def fetch(self) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for page in range(1, self.source.max_pages + 1):
            url = self._build_page_url(page)
            response = self.session.get(url, timeout=self.source.request_timeout_sec)
            if response.status_code >= 400:
                raise ProviderError(f"Avito responded with HTTP {response.status_code} for {url}")

            html = response.text
            self._ensure_not_suspicious(html=html, page=page, url=url)
            page_items = self.parse_listings_from_html(source_key=self.source.source_key, html=html)

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

        return listings

    @classmethod
    def parse_listings_from_html(cls, *, source_key: str, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[data-marker="item"][data-item-id]')
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
                    image_url=image_url,
                    photo_urls=photo_urls,
                    raw_payload={
                        "specific_params": specific_params,
                        "published_text": published_text,
                        "address_lines": address_lines,
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
        if page <= 1:
            return self.source.search_url

        parsed = urlparse(self.source.search_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["p"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(query)))

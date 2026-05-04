from pathlib import Path

import pytest

from hata_bot.exceptions import SuspiciousResponseError
from hata_bot.models import SourceConfig
from hata_bot.providers.avito import AvitoProvider


FIXTURE = Path("tests/fixtures/avito_search_sample.html").read_text(encoding="utf-8")


def make_source() -> SourceConfig:
    return SourceConfig(
        source_key="avito_nsk_family",
        display_name="Авито",
        provider="avito",
        enabled=True,
        search_url="https://www.avito.ru/novosibirsk/kvartiry/sdam-ASgBAgICAUSSA8gQ?s=104",
        max_pages=1,
        request_timeout_sec=20,
        repost_suppression_days=30,
        user_agent="pytest-agent",
        poll_note="test",
    )


def test_parse_listings_from_html_extracts_expected_fields() -> None:
    listings = AvitoProvider.parse_listings_from_html(source_key="avito_nsk_family", html=FIXTURE)

    assert len(listings) == 5
    first = listings[0]
    assert first.external_id == "7980772823"
    assert first.title == "3-к. квартира, 104,4 м², 14/21 эт."
    assert first.price_rub == 80000
    assert first.rooms == 3
    assert first.area_m2 == 104.4
    assert first.address == "ул. Орджоникидзе, 47"
    assert first.metro == "Площадь Ленина, 11–15 мин."
    assert first.published_text == "1 час назад"
    assert first.image_url == "https://00.img.avito.st/image/1/1.first-card-photo.jpg"
    assert first.photo_urls[0] == first.image_url
    assert first.url.startswith("https://www.avito.ru/novosibirsk/kvartiry/")


def test_duplicate_cards_with_different_avito_ids_share_same_fingerprint() -> None:
    listings = AvitoProvider.parse_listings_from_html(source_key="avito_nsk_family", html=FIXTURE)
    duplicate_a = listings[2]
    duplicate_b = listings[3]

    assert duplicate_a.external_id != duplicate_b.external_id
    assert duplicate_a.content_fingerprint == duplicate_b.content_fingerprint


def test_fetch_rejects_suspicious_antibot_response() -> None:
    class FakeResponse:
        status_code = 200
        text = "<html><body>Подтвердите, что вы не робот</body></html>"

    class FakeSession:
        def get(self, url, timeout):
            return FakeResponse()

    provider = AvitoProvider(make_source(), session=FakeSession())
    with pytest.raises(SuspiciousResponseError):
        provider.fetch()


def test_fetch_accepts_regular_listing_page_even_if_it_mentions_recaptcha() -> None:
    class FakeResponse:
        status_code = 200
        text = FIXTURE + "<script>window.settings={recaptcha_enabled:true}</script>"

    class FakeSession:
        def get(self, url, timeout):
            return FakeResponse()

    provider = AvitoProvider(make_source(), session=FakeSession())
    listings = provider.fetch()

    assert len(listings) == 5

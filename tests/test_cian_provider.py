from pathlib import Path

from hata_bot.models import SourceConfig
from hata_bot.providers.cian import CianProvider


FIXTURE = Path("tests/fixtures/cian_search_sample.html").read_text(encoding="utf-8")


def make_source() -> SourceConfig:
    return SourceConfig(
        source_key="cian_nsk_family",
        display_name="ЦИАН",
        provider="cian",
        enabled=True,
        search_url="https://novosibirsk.cian.ru/cat.php?deal_type=rent",
        max_pages=1,
        request_timeout_sec=30,
        repost_suppression_days=30,
        user_agent="pytest-agent",
        poll_note="ЦИАН test",
        required_districts=["Центральный", "Октябрьский"],
        exclude_text_patterns=["На несколько месяцев"],
        sort_override="creation_date_desc",
    )


def test_parse_listings_from_html_extracts_expected_fields() -> None:
    listings = CianProvider.parse_listings_from_html(source=make_source(), html=FIXTURE)

    assert len(listings) == 2
    first = listings[0]
    assert first.external_id == "321654987"
    assert first.title == "3-комн. квартира, 104,4 м², 14/21 этаж"
    assert first.price_rub == 80000
    assert first.rooms == 3
    assert first.area_m2 == 104.4
    assert first.address == "ул. Орджоникидзе, 47"
    assert first.metro == "Площадь Ленина, 11–15 мин."
    assert first.published_text == "4 часа назад"
    assert first.seller_kind == "agency"
    assert first.seller_name == "БК НЕДВИЖИМОСТЬ"
    assert first.image_url == "https://images.cdn-cian.ru/1/first-photo.jpg"
    assert first.photo_urls == [
        "https://images.cdn-cian.ru/1/first-photo.jpg",
        "https://images.cdn-cian.ru/1/second-photo.jpg",
    ]
    assert first.raw_payload["district"] == "Центральный"
    assert listings[1].seller_kind == "owner"
    assert listings[1].seller_name == "Анна"


def test_build_search_url_applies_sort_override() -> None:
    provider = CianProvider(make_source(), data_dir=Path("tests/.tmp"))

    url = provider._build_search_url()

    assert "sort=creation_date_desc" in url

from pathlib import Path

from hata_bot.models import SourceConfig
from hata_bot.providers.domclick import DomclickProvider


FIXTURE = Path("tests/fixtures/domclick_search_sample.html").read_text(encoding="utf-8")


def make_source() -> SourceConfig:
    return SourceConfig(
        source_key="domclick_nsk_family",
        display_name="Домклик",
        provider="domclick",
        enabled=True,
        search_url="https://novosibirsk.domclick.ru/search?deal_type=rent&category=living&offer_type=flat&offset=0",
        max_pages=2,
        request_timeout_sec=40,
        repost_suppression_days=30,
        user_agent="pytest-agent",
        poll_note="Домклик test",
        min_price_rub=45000,
        max_price_rub=85000,
        min_area_m2=60,
        min_rooms=3,
        required_districts=["Октябрьский", "Центральный", "Железнодорожный"],
    )


def test_parse_listings_from_html_extracts_expected_fields() -> None:
    listings = DomclickProvider.parse_listings_from_html(source=make_source(), html=FIXTURE)

    assert len(listings) == 2
    first = listings[0]
    assert first.external_id == "111"
    assert first.title == "3-комн. квартира, 73 м², 12/25 эт."
    assert first.price_rub == 60000
    assert first.rooms == 3
    assert first.area_m2 == 73.0
    assert first.address == "Новосибирск, улица Лескова, 35"
    assert first.metro == "Октябрьская, ~16 мин."
    assert first.raw_payload["district"] == "Октябрьский"
    assert first.seller_kind == "agency"
    assert first.seller_name == "Недвижимость Хоум НСК"
    assert first.image_url == "https://img.dmclk.ru/c960x640q80/vitrina/ok1.jpg"
    assert first.photo_urls[1] == "https://img.dmclk.ru/c960x640q80/vitrina/ok2.jpg"
    assert first.published_text is not None

    second = listings[1]
    assert second.external_id == "444"
    assert second.raw_payload["district"] == "Железнодорожный"
    assert second.metro == "Красный проспект, ~6 мин."
    assert second.seller_kind == "owner"
    assert second.seller_name == "Анастасия"


def test_build_search_url_uses_offset_pagination() -> None:
    provider = DomclickProvider(make_source(), data_dir=Path("tests/.tmp"))

    url = provider._build_search_url(offset=20)

    assert "offset=20" in url


def test_extract_state_handles_context_marker_inside_json_string() -> None:
    html = """
    <html>
      <head>
        <script>
          window.__SSR_STATE__={"search":{"pages":{"0":{"ids":[1],"entities":{"1":{"id":1,"path":"https://novosibirsk.domclick.ru/card/rent__flat__1","description":"marker window.__SSR_CONTEXT__= should stay inside json"}}}}}};
          window.__SSR_CONTEXT__={};
        </script>
      </head>
    </html>
    """

    state = DomclickProvider._extract_state(html)

    entity = state["search"]["pages"]["0"]["entities"]["1"]
    assert entity["description"] == "marker window.__SSR_CONTEXT__= should stay inside json"

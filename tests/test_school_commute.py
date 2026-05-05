from pathlib import Path

from hata_bot.models import Listing, SchoolCommuteConfig
from hata_bot.school_commute import SchoolCommuteService
from hata_bot.state import StateStore


def make_listing() -> Listing:
    return Listing(
        source_key="avito_nsk_family",
        external_id="123",
        url="https://example.com/123",
        title="3-к. квартира, 104,4 м², 14/21 эт.",
        price_rub=80000,
        rooms=3,
        area_m2=104.4,
        address="ул. Орджоникидзе, 47",
        metro="Площадь Ленина, 11–15 мин.",
        published_text="1 час назад",
        content_fingerprint="fp-1",
        raw_payload={"district": "Центральный"},
    )


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, params, timeout))
        return FakeResponse(
            200,
            {
                "result": {
                    "items": [
                        {
                            "full_name": "Новосибирск, ул. Орджоникидзе, 47",
                            "point": {
                                "lat": 55.030123,
                                "lon": 82.920456,
                            },
                        }
                    ]
                }
            },
        )

    def post(self, url, params=None, json=None, timeout=None):
        self.post_calls.append((url, params, json, timeout))
        if "public_transport" in url:
            return FakeResponse(
                200,
                [
                    {
                        "pedestrian": False,
                        "total_duration": 29 * 60,
                        "total_distance": 6100,
                    }
                ],
            )

        if json and json.get("transport") == "walking":
            return FakeResponse(
                200,
                {
                    "result": [
                        {
                            "duration": 21 * 60,
                            "length": 1700,
                        }
                    ]
                },
            )

        return FakeResponse(
            200,
            {
                "result": [
                    {
                        "duration": 12 * 60,
                        "length": 4800,
                    }
                ]
            },
        )


def build_state(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "data" / "hatabot.db")
    state.initialize()
    return state


def test_school_commute_enriches_listing_and_reuses_cache(tmp_path: Path) -> None:
    state = build_state(tmp_path)
    session = FakeSession()
    service = SchoolCommuteService(
        SchoolCommuteConfig(
            enabled=True,
            api_key="demo-key",
            origin_city_name="Новосибирск",
            destination_name="Школа ребенка",
            destination_address="Спартака, 12",
            destination_lat=55.020413,
            destination_lon=82.922467,
            departure_time_local="07:30",
            cache_ttl_hours=168,
            request_timeout_sec=10,
        ),
        state,
        session=session,
    )

    enriched = service.enrich_listing(make_listing())

    assert enriched.school_commute is not None
    assert enriched.school_commute.walking is not None
    assert enriched.school_commute.walking.duration_sec == 21 * 60
    assert enriched.school_commute.driving is not None
    assert enriched.school_commute.driving.duration_sec == 12 * 60
    assert enriched.school_commute.transit is not None
    assert enriched.school_commute.transit.duration_sec == 29 * 60
    assert len(session.get_calls) == 1
    assert len(session.post_calls) == 3

    second = service.enrich_listing(make_listing())

    assert second.school_commute is not None
    assert len(session.get_calls) == 1
    assert len(session.post_calls) == 3
    state.close()

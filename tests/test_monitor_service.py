import logging
from dataclasses import replace
from pathlib import Path

import pytest

from hata_bot.models import AppConfig, Listing, RunResult, Settings, SourceConfig, TelegramConfig
from hata_bot.notifiers.base import Notifier
from hata_bot.services.monitor import MonitorService
from hata_bot.state import StateStore


def make_source() -> SourceConfig:
    return SourceConfig(
        source_key="avito_nsk_family",
        display_name="Авито",
        provider="avito",
        enabled=True,
        search_url="https://example.com/search",
        max_pages=1,
        request_timeout_sec=20,
        repost_suppression_days=30,
        user_agent="pytest-agent",
        poll_note="Авито test",
    )


def make_settings(tmp_path: Path) -> Settings:
    app = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        database_file=tmp_path / "data" / "hatabot.db",
        lock_file=tmp_path / "data" / "hatabot.lock",
    )
    telegram = TelegramConfig(enabled=True, bot_token="token", chat_id="chat")
    return Settings(app=app, telegram=telegram, sources=[make_source()])


def make_listing(external_id: str, title: str, price: int, address: str, fingerprint: str) -> Listing:
    return Listing(
        source_key="avito_nsk_family",
        external_id=external_id,
        url=f"https://example.com/{external_id}",
        title=title,
        price_rub=price,
        rooms=3,
        area_m2=70.9,
        address=address,
        metro="Маршала Покрышкина, 6–10 мин.",
        published_text="1 час назад",
        content_fingerprint=fingerprint,
        image_url=f"https://images.example.com/{external_id}.jpg",
        photo_urls=[f"https://images.example.com/{external_id}.jpg"],
        raw_payload={"note": "test"},
    )


class FakeNotifier(Notifier):
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.sent_ids: list[str] = []

    def send_new_listing(self, listing: Listing, *, poll_note: str | None = None) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary telegram outage")
        self.sent_ids.append(listing.external_id)

    def send_test_message(self, message: str) -> None:
        return None

    def healthcheck(self) -> str:
        return "fake"


class FakeProvider:
    def __init__(self, listings: list[Listing]) -> None:
        self.listings = listings

    def fetch(self) -> list[Listing]:
        return list(self.listings)


def build_service(tmp_path: Path, provider: FakeProvider, notifier: FakeNotifier):
    settings = make_settings(tmp_path)
    state = StateStore(settings.app.database_file)
    state.initialize()
    service = MonitorService(
        settings,
        state,
        notifier,
        logger=logging.getLogger("test.monitor"),
        provider_factory=lambda source: provider,
    )
    return settings, state, service


def test_first_run_bootstraps_silently(tmp_path: Path) -> None:
    listing = make_listing("1001", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "fp-1")
    settings, state, service = build_service(tmp_path, FakeProvider([listing]), FakeNotifier())

    result = service.run_source(settings.sources[0])

    assert result.status == "bootstrap"
    assert result.bootstrap is True
    assert result.new_count == 0
    assert state.count_seen("avito_nsk_family") == 1
    assert state.get_pending_notifications("avito_nsk_family") == []
    state.close()


def test_second_run_sends_only_new_listing(tmp_path: Path) -> None:
    baseline = make_listing("1001", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "fp-1")
    new_listing = make_listing("1002", "3-к. квартира, 75 м², 9/12 эт.", 60000, "ул. Есенина, 67", "fp-2")
    notifier = FakeNotifier()
    settings, state, service = build_service(tmp_path, FakeProvider([baseline]), notifier)

    service.run_source(settings.sources[0])
    service.provider_factory = lambda source: FakeProvider([baseline, new_listing])
    result = service.run_source(settings.sources[0])

    assert result.status == "ok"
    assert result.new_count == 1
    assert notifier.sent_ids == ["1002"]
    state.close()


def test_repost_duplicate_is_suppressed_by_fingerprint(tmp_path: Path) -> None:
    original = make_listing("1001", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "same-fp")
    repost = make_listing("9999", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "same-fp")
    notifier = FakeNotifier()
    settings, state, service = build_service(tmp_path, FakeProvider([original]), notifier)

    service.run_source(settings.sources[0])
    service.provider_factory = lambda source: FakeProvider([original, repost])
    result = service.run_source(settings.sources[0])

    assert result.new_count == 0
    row = state.get_listing_row("avito_nsk_family", "9999")
    assert row is not None
    assert row["notification_state"] == "suppressed"
    state.close()


def test_pending_notification_survives_temporary_failure(tmp_path: Path) -> None:
    baseline = make_listing("1001", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "fp-1")
    new_listing = make_listing("1002", "3-к. квартира, 75 м², 9/12 эт.", 60000, "ул. Есенина, 67", "fp-2")
    failing_notifier = FakeNotifier(fail_once=True)
    settings, state, service = build_service(tmp_path, FakeProvider([baseline]), failing_notifier)

    service.run_source(settings.sources[0])
    service.provider_factory = lambda source: FakeProvider([baseline, new_listing])

    with pytest.raises(RuntimeError):
        service.run_source(settings.sources[0])

    assert state.get_pending_notifications("avito_nsk_family")[0].external_id == "1002"

    success_notifier = FakeNotifier()
    service.notifier = success_notifier
    result = service.run_source(settings.sources[0])

    assert result.new_count == 1
    assert success_notifier.sent_ids == ["1002"]
    assert state.get_pending_notifications("avito_nsk_family") == []
    state.close()


def test_profile_change_reseeds_baseline_without_old_notifications(tmp_path: Path) -> None:
    baseline = make_listing("1001", "3-к. квартира, 70,9 м², 6/24 эт.", 85000, "ул. Державина, 47", "fp-1")
    old_match = make_listing("1002", "3-к. квартира, 75 м², 9/12 эт.", 60000, "ул. Есенина, 67", "fp-2")
    notifier = FakeNotifier()
    settings, state, service = build_service(tmp_path, FakeProvider([baseline]), notifier)

    service.run_source(settings.sources[0])
    service.provider_factory = lambda source: FakeProvider([baseline, old_match])

    changed_source = replace(settings.sources[0], min_price_rub=45000, max_price_rub=85000, min_area_m2=60, min_rooms=3)
    result = service.run_source(changed_source)

    assert result.status == "bootstrap"
    assert result.bootstrap is True
    assert result.bootstrap_reason == "profile_changed"
    assert result.new_count == 0
    assert notifier.sent_ids == []
    assert state.get_pending_notifications("avito_nsk_family") == []
    state.close()

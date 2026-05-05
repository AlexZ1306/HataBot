import logging
from pathlib import Path

from hata_bot.models import AppConfig, Listing, RunResult, Settings, SourceConfig, TelegramConfig
from hata_bot.services.telegram_control import (
    BUTTON_CHECK_ALL,
    BUTTON_MENU,
    BUTTON_CHECK_NOW,
    BUTTON_LAST,
    BUTTON_LAST_THREE,
    TelegramControlBot,
)
from hata_bot.state import StateStore


def make_source(*, source_key: str = "avito_nsk_family", display_name: str = "Авито", provider: str = "avito") -> SourceConfig:
    return SourceConfig(
        source_key=source_key,
        display_name=display_name,
        provider=provider,
        enabled=True,
        search_url="https://example.com/search",
        max_pages=1,
        request_timeout_sec=20,
        repost_suppression_days=30,
        user_agent="pytest-agent",
        poll_note=f"{display_name} test",
    )


def make_settings(tmp_path: Path) -> Settings:
    app = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        database_file=tmp_path / "data" / "hatabot.db",
        lock_file=tmp_path / "data" / "hatabot.lock",
    )
    telegram = TelegramConfig(enabled=True, bot_token="token", chat_id="1281297580")
    return Settings(
        app=app,
        telegram=telegram,
        sources=[
            make_source(),
            make_source(source_key="cian_nsk_family", display_name="ЦИАН", provider="cian"),
        ],
    )


def make_listing(external_id: str) -> Listing:
    return Listing(
        source_key="avito_nsk_family",
        external_id=external_id,
        url=f"https://example.com/{external_id}",
        title=f"3-к. квартира, {70 + int(external_id)} м², 9/12 эт.",
        price_rub=60000 + int(external_id),
        rooms=3,
        area_m2=70.0 + int(external_id),
        address=f"ул. Тестовая, {external_id}",
        metro="Площадь Ленина, 5 мин.",
        published_text="1 час назад",
        content_fingerprint=f"fp-{external_id}",
        image_url=f"https://images.example.com/{external_id}.jpg",
        photo_urls=[f"https://images.example.com/{external_id}.jpg"],
        raw_payload={},
    )


class FakeTelegramNotifier:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []
        self.sent_listings: list[dict] = []

    def send_message(self, text: str, *, chat_id: str | None = None, reply_markup: dict | None = None) -> None:
        self.sent_messages.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})

    def send_new_listing(self, listing: Listing, *, poll_note: str | None = None) -> None:
        self.sent_messages.append({"text": f"listing:{listing.external_id}", "chat_id": None, "reply_markup": None})

    def send_listing(self, listing: Listing, *, poll_note: str | None = None, chat_id: str | None = None) -> None:
        self.sent_listings.append({"listing": listing, "poll_note": poll_note, "chat_id": chat_id})

    def get_updates(self, *, offset=None, timeout=25):
        return []


class FakeControlBot(TelegramControlBot):
    def __init__(self, *args, latest_listings=None, run_results=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._latest_listings = latest_listings or []
        self._run_results = run_results or []

    def run_monitor_now(self, *, source_key: str | None = None):
        return list(self._run_results)

    def fetch_latest_listings(self, *, source: SourceConfig, limit: int):
        return self._latest_listings[:limit]


def build_bot(tmp_path: Path, *, latest_listings=None, run_results=None):
    settings = make_settings(tmp_path)
    state = StateStore(settings.app.database_file)
    state.initialize()
    notifier = FakeTelegramNotifier()
    bot = FakeControlBot(
        settings,
        notifier,
        state,
        logger=logging.getLogger("test.telegram_control"),
        latest_listings=latest_listings,
        run_results=run_results,
    )
    return bot, notifier, state


def test_start_message_sends_menu(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)
    bot.handle_message(chat_id="1281297580", text="/start")

    assert "<b>HataBot</b>" in notifier.sent_messages[-1]["text"]
    buttons = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert buttons[0][0]["text"] == BUTTON_CHECK_ALL
    assert buttons[1][0]["text"] == "Авито"
    assert buttons[2][0]["text"] == "ЦИАН"
    state.close()


def test_check_now_requires_source_selection(tmp_path: Path) -> None:
    results = [RunResult("avito_nsk_family", "ok", 50, 0, False, None)]
    bot, notifier, state = build_bot(tmp_path, run_results=results)

    bot.handle_message(chat_id="1281297580", text=BUTTON_CHECK_NOW)

    assert "Сначала выбери источник" in notifier.sent_messages[-1]["text"]
    state.close()


def test_selecting_source_switches_to_source_menu(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_message(chat_id="1281297580", text="ЦИАН")

    assert "<b>ЦИАН</b>" in notifier.sent_messages[-1]["text"]
    rows = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert rows[0][0]["text"] == "Проверить ЦИАН"
    assert rows[-1][0]["text"] == BUTTON_MENU
    assert state.get_meta("telegram_selected_source") == "cian_nsk_family"
    state.close()


def test_check_now_reports_no_new_items_for_selected_source(tmp_path: Path) -> None:
    results = [RunResult("cian_nsk_family", "ok", 17, 0, False, None, scanned_count=21, matched_count=17)]
    bot, notifier, state = build_bot(tmp_path, run_results=results)
    bot.handle_message(chat_id="1281297580", text="ЦИАН")

    bot.handle_message(chat_id="1281297580", text="Проверить ЦИАН")

    assert "<b>ЦИАН</b>" in notifier.sent_messages[-1]["text"]
    assert "Новых объявлений нет" in notifier.sent_messages[-1]["text"]
    assert "Просмотрел 21, подошло 17" in notifier.sent_messages[-1]["text"]
    state.close()


def test_latest_buttons_return_listing_text_for_selected_source(tmp_path: Path) -> None:
    listings = [make_listing("1"), make_listing("2"), make_listing("3")]
    bot, notifier, state = build_bot(tmp_path, latest_listings=listings)
    bot.handle_message(chat_id="1281297580", text="Авито")

    bot.handle_message(chat_id="1281297580", text=BUTTON_LAST)
    assert notifier.sent_listings[-1]["listing"].external_id == "1"
    assert "Авито: последнее объявление" in notifier.sent_listings[-1]["poll_note"]

    bot.handle_message(chat_id="1281297580", text=BUTTON_LAST_THREE)
    assert "Показываю 3 самых свежих объявления из Авито" in notifier.sent_messages[-1]["text"]
    assert [item["listing"].external_id for item in notifier.sent_listings[-3:]] == ["1", "2", "3"]
    assert notifier.sent_listings[-1]["poll_note"] == "Авито: объявление 3 из 3"
    state.close()


def test_menu_button_returns_to_source_picker(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)
    bot.handle_message(chat_id="1281297580", text="Авито")

    bot.handle_message(chat_id="1281297580", text=BUTTON_MENU)

    assert "Можно сразу проверить все сервисы" in notifier.sent_messages[-1]["text"]
    assert state.get_meta("telegram_selected_source") == ""
    state.close()


def test_check_all_builds_compact_summary(tmp_path: Path) -> None:
    results = [
        RunResult("avito_nsk_family", "ok", 50, 0, False, None, scanned_count=50, matched_count=50),
        RunResult("cian_nsk_family", "ok", 17, 2, False, None, scanned_count=31, matched_count=17),
    ]
    bot, notifier, state = build_bot(tmp_path, run_results=results)

    bot.handle_message(chat_id="1281297580", text=BUTTON_CHECK_ALL)

    text = notifier.sent_messages[-1]["text"]
    assert "Проверка завершена" in text
    assert "Авито: новых объявлений нет. Подходящих сейчас: 50." in text
    assert "ЦИАН: новых объявлений 2. Просмотрел 31, подошло 17." in text
    assert "Новые объявления уже отправил отдельными сообщениями." in text
    state.close()


def test_unauthorized_chat_gets_help_message_with_chat_id(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 777777},
                "text": "/start",
            },
        }
    )

    text = notifier.sent_messages[-1]["text"]
    assert "Этот чат пока не подключён" in text
    assert "777777" in text
    state.close()

import logging
from pathlib import Path

from hata_bot.models import AppConfig, Listing, RunResult, Settings, SourceConfig, TelegramConfig
from hata_bot.search_profile import load_search_profile
from hata_bot.services.telegram_control import (
    BUTTON_CHECK_ALL,
    BUTTON_CHECK_NOW,
    BUTTON_EDIT_DISTRICTS,
    BUTTON_EDIT_MIN_PRICE,
    BUTTON_LAST,
    BUTTON_LAST_THREE,
    BUTTON_MENU,
    BUTTON_RANDOM_PICK,
    BUTTON_SETTINGS,
    BUTTON_SETTINGS_CHAT,
    BUTTON_SETTINGS_OPEN_WEB,
    BUTTON_SETTINGS_WEB,
    RANDOM_PICKER_KEY,
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
    def __init__(self, *args, latest_listings=None, run_results=None, random_candidates=None, webapp_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._latest_listings = latest_listings or []
        self._run_results = run_results or []
        self._random_candidates = random_candidates or []
        self._webapp_url = webapp_url

    def run_monitor_now(self, *, source_key: str | None = None):
        return list(self._run_results)

    def fetch_latest_listings(self, *, source: SourceConfig, limit: int):
        return self._latest_listings[:limit]

    def fetch_random_candidate_pool(self):
        return list(self._random_candidates)

    def _ensure_random_pool_refresh_async(self, *, force: bool = False) -> None:
        return None

    def _build_settings_webapp_url(self) -> str | None:
        return self._webapp_url


def build_bot(tmp_path: Path, *, latest_listings=None, run_results=None, random_candidates=None, webapp_url=None):
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
        random_candidates=random_candidates,
        webapp_url=webapp_url,
    )
    return bot, notifier, state


def test_start_message_sends_menu(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)
    bot.handle_message(chat_id="1281297580", text="/start")

    assert "<b>HataBot</b>" in notifier.sent_messages[-1]["text"]
    buttons = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert buttons[0][0]["text"] == BUTTON_CHECK_ALL
    assert buttons[1][0]["text"] == BUTTON_RANDOM_PICK
    assert buttons[2][0]["text"] == "Авито"
    assert buttons[3][0]["text"] == "ЦИАН"
    assert buttons[4][0]["text"] == BUTTON_SETTINGS
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
    assert state.get_meta("telegram_selected_source:1281297580") == "cian_nsk_family"
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
    assert state.get_meta("telegram_selected_source:1281297580") == ""
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


def test_settings_menu_shows_search_profile_summary(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS)

    text = notifier.sent_messages[-1]["text"]
    rows = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert "Как удобнее изменить параметры" in text
    assert rows[0][0]["text"] == BUTTON_SETTINGS_CHAT
    assert rows[0][1]["text"] == BUTTON_SETTINGS_WEB
    state.close()


def test_settings_chat_choice_opens_existing_chat_flow(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS)
    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS_CHAT)

    text = notifier.sent_messages[-1]["text"]
    rows = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert "Город: Новосибирск" in text
    assert rows[0][0]["text"] == BUTTON_EDIT_DISTRICTS
    state.close()


def test_settings_web_choice_opens_webapp_button(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path, webapp_url="https://example.com/webapp")

    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS)
    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS_WEB)

    rows = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    assert rows[0][0]["text"] == BUTTON_SETTINGS_OPEN_WEB
    assert rows[0][0]["web_app"]["url"] == "https://example.com/webapp"
    state.close()


def test_web_app_data_updates_profile(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 1281297580},
                "web_app_data": {
                    "data": '{"type":"settings_form_submit","payload":{"districts":["Центральный","Октябрьский"],"min_price_rub":"50000","max_price_rub":"90000","min_area_m2":"65","min_rooms":"4","enabled_source_keys":["cian_nsk_family"]}}'
                },
            },
        }
    )

    profile = load_search_profile(bot.settings, state)
    assert profile.districts == ["Октябрьский", "Центральный"]
    assert profile.min_price_rub == 50000
    assert profile.max_price_rub == 90000
    assert profile.min_area_m2 == 65.0
    assert profile.min_rooms == 4
    assert profile.enabled_source_keys == ["cian_nsk_family"]
    assert "Настройки обновил" in notifier.sent_messages[-1]["text"]
    state.close()


def test_settings_can_update_min_price(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS)
    bot.handle_message(chat_id="1281297580", text=BUTTON_EDIT_MIN_PRICE)
    bot.handle_message(chat_id="1281297580", text="55000")

    text = notifier.sent_messages[-1]["text"]
    assert "Минимальную цену обновил" in text
    assert "от 55 000 ₽" in text
    state.close()


def test_district_settings_show_expanded_novosibirsk_districts(tmp_path: Path) -> None:
    bot, notifier, state = build_bot(tmp_path)

    bot.handle_message(chat_id="1281297580", text=BUTTON_SETTINGS)
    bot.handle_message(chat_id="1281297580", text=BUTTON_EDIT_DISTRICTS)

    keyboard = notifier.sent_messages[-1]["reply_markup"]["keyboard"]
    labels = [button["text"] for row in keyboard for button in row]
    assert any("Заельцовский" in label for label in labels)
    assert any("Калининский" in label for label in labels)
    assert any("Кировский" in label for label in labels)
    assert any("Ленинский" in label for label in labels)
    assert any("Первомайский" in label for label in labels)
    assert any("Советский" in label for label in labels)
    state.close()


def test_random_pick_returns_listing_and_remembers_it(tmp_path: Path) -> None:
    listing = make_listing("7")
    bot, notifier, state = build_bot(tmp_path)
    state.replace_picker_candidates(
        picker_key=RANDOM_PICKER_KEY,
        listings=[listing],
        refreshed_at="2026-05-05T00:00:00+00:00",
    )

    bot.handle_message(chat_id="1281297580", text=BUTTON_RANDOM_PICK)

    assert notifier.sent_listings[-1]["listing"].external_id == "7"
    assert "Случайный вариант из общего пула" in notifier.sent_listings[-1]["poll_note"]
    assert listing.content_fingerprint in state.get_picker_seen_keys(RANDOM_PICKER_KEY)
    row = state.get_listing_row(listing.source_key, listing.external_id)
    assert row is not None
    assert row["notification_state"] == "suppressed"
    state.close()

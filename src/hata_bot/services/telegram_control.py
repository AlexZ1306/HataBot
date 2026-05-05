from __future__ import annotations

import logging
import random
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hata_bot.exceptions import ConfigError, SingleInstanceError
from hata_bot.locking import SingleInstanceLock
from hata_bot.models import Listing, RunResult, SearchProfile, Settings, SourceConfig
from hata_bot.notifiers.telegram import TelegramNotifier
from hata_bot.school_commute import SchoolCommuteService
from hata_bot.search_profile import (
    ROOM_OPTIONS,
    SUPPORTED_DISTRICTS,
    apply_search_profile_to_source,
    default_search_profile,
    format_district_button,
    format_search_profile_summary,
    format_source_button,
    load_search_profile,
    parse_toggle_label,
    save_search_profile,
)
from hata_bot.services.monitor import MonitorService
from hata_bot.state import StateStore, utc_now_iso


BUTTON_CHECK_ALL = "Проверить новые объявления"
BUTTON_RANDOM_PICK = "Подобрать вариант"
BUTTON_CHECK_NOW = "Проверить выбранный сервис"
BUTTON_LAST = "Последнее объявление"
BUTTON_LAST_THREE = "Последние 3 объявления"
BUTTON_MENU = "Меню"
BUTTON_SETTINGS = "Настройки"
BUTTON_SETTINGS_BACK = "К настройкам"
BUTTON_EDIT_DISTRICTS = "Районы"
BUTTON_EDIT_MIN_PRICE = "Цена от"
BUTTON_EDIT_MAX_PRICE = "Цена до"
BUTTON_EDIT_ROOMS = "Комнаты"
BUTTON_EDIT_AREA = "Площадь"
BUTTON_EDIT_SOURCES = "Источники"
BUTTON_RESET_SETTINGS = "Сбросить настройки"
BUTTON_CONFIRM_RESET = "Да, сбросить"
BUTTON_CLEAR_LIMIT = "Убрать лимит"

UPDATE_OFFSET_KEY = "telegram_update_offset"
SELECTED_SOURCE_KEY = "telegram_selected_source"
UI_MODE_KEY = "telegram_ui_mode"

UI_MODE_NONE = ""
UI_MODE_SETTINGS = "settings"
UI_MODE_SETTINGS_DISTRICTS = "settings_districts"
UI_MODE_SETTINGS_SOURCES = "settings_sources"
UI_MODE_SETTINGS_ROOMS = "settings_rooms"
UI_MODE_AWAIT_MIN_PRICE = "await_min_price"
UI_MODE_AWAIT_MAX_PRICE = "await_max_price"
UI_MODE_AWAIT_MIN_AREA = "await_min_area"
UI_MODE_CONFIRM_RESET = "confirm_reset"

RANDOM_PICKER_KEY = "random_global_v1"
RANDOM_PICK_PAGE_LIMITS = {
    "avito": (6, 4, 2, 1),
    "cian": (8, 5, 3, 2, 1),
    "domclick": (8, 5, 3, 2, 1),
}
RANDOM_PICK_CACHE_TTL = timedelta(hours=6)


class TelegramControlBot:
    def __init__(
        self,
        settings: Settings,
        notifier: TelegramNotifier,
        state: StateStore,
        *,
        logger: logging.Logger | None = None,
        provider_factory=None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.state = state
        self.logger = logger or logging.getLogger("hata_bot.telegram_control")
        self.provider_factory = provider_factory
        self.allowed_chat_ids = self._resolve_allowed_chat_ids()
        self.listener_lock_file = settings.app.lock_file.with_name("hatabot-telegram-listener.lock")
        self._random_pool_refresh_lock = threading.Lock()
        self._random_pool_refresh_started = False
        self.school_commute = SchoolCommuteService(settings.school_commute, state, logger=self.logger)

    def poll_forever(self, *, poll_timeout: int = 25, once: bool = False) -> None:
        offset = self._load_offset()
        while True:
            updates = self.notifier.get_updates(offset=offset, timeout=0 if once else poll_timeout)
            if not updates:
                if once:
                    return
                continue

            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    self.handle_update(update)
                except Exception:
                    self.logger.exception("Failed to handle Telegram update %s", update.get("update_id"))
                finally:
                    self.state.set_meta(UPDATE_OFFSET_KEY, str(offset))

            if once:
                return

    def handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        text = message.get("text")
        if not isinstance(text, str):
            return

        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if chat_id not in self.allowed_chat_ids:
            self.logger.warning("Received Telegram update from unauthorized chat_id=%s", chat_id)
            self._handle_unauthorized_chat(chat_id=chat_id)
            return

        self.handle_message(chat_id=chat_id, text=text)

    def handle_message(self, *, chat_id: str, text: str) -> None:
        normalized = text.strip().casefold()

        if normalized in {"/start", "/help", "/menu", BUTTON_MENU.casefold(), "start"}:
            self._clear_chat_context(chat_id)
            self._send_main_menu(chat_id)
            return

        if self._handle_settings_flow(chat_id=chat_id, text=text):
            return

        if normalized in {BUTTON_SETTINGS.casefold(), "/settings"}:
            self._set_ui_mode(chat_id, UI_MODE_SETTINGS)
            self._send_settings_menu(chat_id)
            return

        if normalized in {BUTTON_RANDOM_PICK.casefold(), "/random_pick", "/pick"}:
            self._clear_selected_source(chat_id)
            self._set_ui_mode(chat_id, UI_MODE_NONE)
            self._handle_random_pick(chat_id)
            return

        source = self._match_source_by_text(text)
        if source is not None:
            self._set_selected_source(chat_id, source)
            self._set_ui_mode(chat_id, UI_MODE_NONE)
            self._send_source_menu(chat_id, source)
            return

        selected_source = self._get_selected_source(chat_id)
        if normalized in {BUTTON_CHECK_ALL.casefold(), "/check_all"}:
            self._clear_selected_source(chat_id)
            self._set_ui_mode(chat_id, UI_MODE_NONE)
            self._handle_check_all(chat_id)
            return
        if normalized in self._check_button_aliases(selected_source):
            self._handle_check_now(chat_id, selected_source)
            return
        if normalized in {BUTTON_LAST.casefold(), "/latest"}:
            self._handle_latest(chat_id, selected_source=selected_source, limit=1)
            return
        if normalized in {BUTTON_LAST_THREE.casefold(), "/latest3", "/latest_3"}:
            self._handle_latest(chat_id, selected_source=selected_source, limit=3)
            return

        reply_markup = self._source_menu(selected_source) if selected_source else self._main_menu()
        self.notifier.send_message(
            "Нажми одну из кнопок ниже.",
            chat_id=chat_id,
            reply_markup=reply_markup,
        )

    def _send_main_menu(self, chat_id: str) -> None:
        self._ensure_random_pool_refresh_async()
        text = (
            "<b>HataBot</b>\n"
            "Можно сразу проверить все сервисы, открыть один сервис отдельно или зайти в настройки."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._main_menu())

    def _send_source_menu(self, chat_id: str, source: SourceConfig) -> None:
        text = (
            f"<b>{source.display_name}</b>\n"
            "Можно проверить новые объявления или посмотреть самые свежие варианты."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._source_menu(source))

    def _send_settings_menu(self, chat_id: str, *, prefix: str | None = None) -> None:
        self._set_ui_mode(chat_id, UI_MODE_SETTINGS)
        text = format_search_profile_summary(self._load_profile(), self._all_sources())
        if prefix:
            text = prefix + "\n\n" + text
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._settings_menu())

    def _send_districts_menu(self, chat_id: str, *, prefix: str | None = None) -> None:
        self._set_ui_mode(chat_id, UI_MODE_SETTINGS_DISTRICTS)
        profile = self._load_profile()
        lines = [
            "<b>Районы</b>",
            "Нажимай на районы, чтобы включать или выключать их.",
            f"Сейчас выбраны: {', '.join(profile.districts)}",
        ]
        if prefix:
            lines.insert(0, prefix)
        self.notifier.send_message("\n".join(lines), chat_id=chat_id, reply_markup=self._districts_menu(profile))

    def _send_sources_menu(self, chat_id: str, *, prefix: str | None = None) -> None:
        self._set_ui_mode(chat_id, UI_MODE_SETTINGS_SOURCES)
        profile = self._load_profile()
        enabled_names = [source.display_name for source in self._all_sources() if source.source_key in profile.enabled_source_keys]
        lines = [
            "<b>Источники</b>",
            "Нажимай на сервис, чтобы включать или выключать его.",
            f"Сейчас включены: {', '.join(enabled_names)}",
        ]
        if prefix:
            lines.insert(0, prefix)
        self.notifier.send_message("\n".join(lines), chat_id=chat_id, reply_markup=self._sources_menu(profile))

    def _send_rooms_menu(self, chat_id: str, *, prefix: str | None = None) -> None:
        self._set_ui_mode(chat_id, UI_MODE_SETTINGS_ROOMS)
        profile = self._load_profile()
        current_value = f"от {profile.min_rooms}" if profile.min_rooms is not None else "от 3"
        lines = [
            "<b>Комнаты</b>",
            "Выбери минимальное число комнат. Сейчас доступны варианты от 3 комнат.",
            f"Сейчас: {current_value}",
        ]
        if prefix:
            lines.insert(0, prefix)
        self.notifier.send_message("\n".join(lines), chat_id=chat_id, reply_markup=self._rooms_menu())

    def _prompt_numeric_value(self, chat_id: str, *, ui_mode: str, title: str, example: str, allow_clear: bool = True) -> None:
        self._set_ui_mode(chat_id, ui_mode)
        lines = [
            f"<b>{title}</b>",
            f"Пришли число сообщением. Например: <code>{example}</code>",
        ]
        if allow_clear:
            lines.append("Если лимит не нужен, нажми «Убрать лимит».")
        self.notifier.send_message("\n".join(lines), chat_id=chat_id, reply_markup=self._numeric_input_menu(allow_clear=allow_clear))

    def _handle_check_all(self, chat_id: str) -> None:
        try:
            results = self.run_monitor_now()
        except SingleInstanceError:
            self.notifier.send_message("Проверка уже идёт прямо сейчас. Попробуй ещё раз через минуту.", chat_id=chat_id)
            return
        except Exception as exc:
            self.notifier.send_message(f"Не получилось проверить объявления: {exc}", chat_id=chat_id)
            return

        text = self._build_all_sources_summary(results)
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._main_menu())

    def _handle_random_pick(self, chat_id: str) -> None:
        listings = self.state.get_picker_candidates(RANDOM_PICKER_KEY)
        cache_refreshed_at = self.state.get_picker_candidates_refreshed_at(RANDOM_PICKER_KEY)
        using_fallback = False

        if self._is_random_pool_stale(cache_refreshed_at):
            self._ensure_random_pool_refresh_async(force=not listings)

        if not listings:
            listings = self._build_random_pick_fallback_pool()
            using_fallback = True

        if not listings:
            self.notifier.send_message(
                "Полный пул вариантов ещё собирается в фоне. Попробуй нажать кнопку чуть позже.",
                chat_id=chat_id,
                reply_markup=self._main_menu(),
            )
            return

        seen_keys = self.state.get_picker_seen_keys(RANDOM_PICKER_KEY)
        unseen = [listing for listing in listings if listing.content_fingerprint not in seen_keys]
        restarted_cycle = False

        if not unseen:
            self.state.clear_picker_seen(RANDOM_PICKER_KEY)
            unseen = listings
            restarted_cycle = True

        listing = random.choice(unseen)
        shown_at = utc_now_iso()
        self._remember_random_pick(listing, shown_at=shown_at)

        if restarted_cycle:
            self.notifier.send_message(
                "Все варианты уже показывал. Начинаю новый круг и выбрал свежий случайный вариант.",
                chat_id=chat_id,
                reply_markup=self._main_menu(),
            )
        elif using_fallback:
            self.notifier.send_message(
                "Показываю вариант из уже собранной базы. Полный широкий пул сейчас тихо обновляю в фоне.",
                chat_id=chat_id,
                reply_markup=self._main_menu(),
            )

        source = self._get_source_by_key(listing.source_key)
        self.notifier.send_listing(
            self.school_commute.enrich_listing(listing),
            poll_note=f"Случайный вариант из общего пула · {source.display_name}",
            chat_id=chat_id,
        )

    def _handle_check_now(self, chat_id: str, selected_source: SourceConfig | None) -> None:
        source = self._require_selected_source(chat_id, selected_source)
        if source is None:
            return

        try:
            results = self.run_monitor_now(source_key=source.source_key)
        except SingleInstanceError:
            self.notifier.send_message("Проверка уже идёт прямо сейчас. Попробуй ещё раз через минуту.", chat_id=chat_id)
            return
        except Exception as exc:
            self.notifier.send_message(f"Не получилось проверить выдачу: {exc}", chat_id=chat_id)
            return

        result = results[0]
        text = self._build_single_source_summary(source, result)
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._source_menu(source))

    def _handle_latest(self, chat_id: str, *, selected_source: SourceConfig | None, limit: int) -> None:
        source = self._require_selected_source(chat_id, selected_source)
        if source is None:
            return

        try:
            listings = self.fetch_latest_listings(source=source, limit=limit)
        except Exception as exc:
            self.notifier.send_message(f"Не получилось получить свежую выдачу: {exc}", chat_id=chat_id)
            return

        if not listings:
            self.notifier.send_message("Сейчас не удалось найти объявления в выдаче.", chat_id=chat_id)
            return

        if limit == 1:
            self.notifier.send_listing(
                self.school_commute.enrich_listing(listings[0]),
                poll_note=f"{source.display_name}: последнее объявление",
                chat_id=chat_id,
            )
        else:
            self.notifier.send_message(
                f"Показываю {limit} самых свежих объявления из {source.display_name}.",
                chat_id=chat_id,
                reply_markup=self._source_menu(source),
            )
            for index, listing in enumerate(listings, start=1):
                self.notifier.send_listing(
                    self.school_commute.enrich_listing(listing),
                    poll_note=f"{source.display_name}: объявление {index} из {limit}",
                    chat_id=chat_id,
                )

    def run_monitor_now(self, *, source_key: str | None = None) -> list[RunResult]:
        with SingleInstanceLock(self.settings.app.lock_file):
            monitor_state = StateStore(self.settings.app.database_file)
            monitor_state.initialize()
            try:
                service = MonitorService(
                    self.settings,
                    monitor_state,
                    self.notifier,
                    logger=self.logger,
                    provider_factory=self.provider_factory,
                )
                return service.run(source_key=source_key)
            finally:
                monitor_state.close()

    def fetch_latest_listings(self, *, source: SourceConfig, limit: int) -> list[Listing]:
        provider = self._build_provider(source)
        listings = provider.fetch()
        return listings[:limit]

    def fetch_random_candidate_pool(self, *, sources: list[SourceConfig] | None = None) -> list[Listing]:
        candidates: list[Listing] = []
        seen_keys: set[str] = set()

        for source in sources or self._enabled_sources():
            listings = self._fetch_random_source_listings(source)
            for listing in listings:
                if listing.content_fingerprint in seen_keys:
                    continue
                seen_keys.add(listing.content_fingerprint)
                candidates.append(listing)

        return candidates

    def _build_random_pick_fallback_pool(self) -> list[Listing]:
        source_keys = [source.source_key for source in self._enabled_sources()]
        listings = self.state.get_seen_listings(source_keys)
        candidates: list[Listing] = []
        seen_keys: set[str] = set()

        for listing in listings:
            if listing.content_fingerprint in seen_keys:
                continue
            seen_keys.add(listing.content_fingerprint)
            candidates.append(listing)

        return candidates

    def _fetch_random_source_listings(self, source: SourceConfig) -> list[Listing]:
        limits = RANDOM_PICK_PAGE_LIMITS.get(source.provider, (max(source.max_pages, 1),))
        attempted_limits: list[int] = []
        last_error: Exception | None = None

        for page_limit in limits:
            effective_limit = max(source.max_pages, page_limit)
            if effective_limit in attempted_limits:
                continue
            attempted_limits.append(effective_limit)

            try:
                wide_source = replace(source, max_pages=effective_limit)
                provider = self._build_provider(wide_source)
                return provider.fetch()
            except Exception as exc:
                last_error = exc
                self.logger.warning(
                    "Random pick pool fetch failed for %s at max_pages=%s: %s",
                    source.source_key,
                    effective_limit,
                    exc,
                )
                continue

        if last_error is not None:
            raise last_error
        return []

    def _ensure_random_pool_refresh_async(self, *, force: bool = False) -> None:
        refreshed_at = self.state.get_picker_candidates_refreshed_at(RANDOM_PICKER_KEY)
        has_cache = bool(self.state.get_picker_candidates(RANDOM_PICKER_KEY))
        if not force and has_cache and not self._is_random_pool_stale(refreshed_at):
            return

        with self._random_pool_refresh_lock:
            if self._random_pool_refresh_started:
                return
            self._random_pool_refresh_started = True

        sources = self._enabled_sources()
        thread = threading.Thread(
            target=self._refresh_random_pool_cache_worker,
            args=(sources,),
            name="hatabot-random-pool-refresh",
            daemon=True,
        )
        thread.start()

    def _refresh_random_pool_cache_worker(self, sources: list[SourceConfig]) -> None:
        worker_state = StateStore(self.settings.app.database_file)
        worker_state.initialize()
        try:
            listings = self.fetch_random_candidate_pool(sources=sources)
            worker_state.replace_picker_candidates(
                picker_key=RANDOM_PICKER_KEY,
                listings=listings,
                refreshed_at=utc_now_iso(),
            )
            self.logger.info("Random pick cache refreshed with %s listings.", len(listings))
        except Exception:
            self.logger.exception("Failed to refresh random pick cache in background.")
        finally:
            worker_state.close()
            with self._random_pool_refresh_lock:
                self._random_pool_refresh_started = False

    @staticmethod
    def _is_random_pool_stale(refreshed_at: str | None) -> bool:
        if not refreshed_at:
            return True
        try:
            refreshed = datetime.fromisoformat(refreshed_at)
        except ValueError:
            return True
        return refreshed < datetime.now(timezone.utc) - RANDOM_PICK_CACHE_TTL

    def _build_provider(self, source: SourceConfig):
        if self.provider_factory is not None:
            return self.provider_factory(source)

        from hata_bot.providers.avito import AvitoProvider
        from hata_bot.providers.cian import CianProvider
        from hata_bot.providers.domclick import DomclickProvider

        if source.provider == "avito":
            return AvitoProvider(source)
        if source.provider == "cian":
            return CianProvider(source, data_dir=self.settings.app.data_dir)
        if source.provider == "domclick":
            return DomclickProvider(source, data_dir=self.settings.app.data_dir)
        raise ConfigError(f"Unsupported provider: {source.provider}")

    def _handle_settings_flow(self, *, chat_id: str, text: str) -> bool:
        normalized = text.strip().casefold()
        ui_mode = self._get_ui_mode(chat_id)

        if normalized == BUTTON_SETTINGS_BACK.casefold():
            self._send_settings_menu(chat_id)
            return True

        if ui_mode == UI_MODE_SETTINGS_DISTRICTS:
            if self._toggle_district(chat_id, text):
                return True
        if ui_mode == UI_MODE_SETTINGS_SOURCES:
            if self._toggle_source(chat_id, text):
                return True
        if ui_mode == UI_MODE_SETTINGS_ROOMS:
            if self._update_rooms(chat_id, text):
                return True
        if ui_mode in {UI_MODE_AWAIT_MIN_PRICE, UI_MODE_AWAIT_MAX_PRICE, UI_MODE_AWAIT_MIN_AREA}:
            return self._handle_numeric_input(chat_id, text, ui_mode)
        if ui_mode == UI_MODE_CONFIRM_RESET:
            return self._handle_reset_confirmation(chat_id, text)

        if normalized == BUTTON_EDIT_DISTRICTS.casefold():
            self._send_districts_menu(chat_id)
            return True
        if normalized == BUTTON_EDIT_SOURCES.casefold():
            self._send_sources_menu(chat_id)
            return True
        if normalized == BUTTON_EDIT_ROOMS.casefold():
            self._send_rooms_menu(chat_id)
            return True
        if normalized == BUTTON_EDIT_MIN_PRICE.casefold():
            self._prompt_numeric_value(chat_id, ui_mode=UI_MODE_AWAIT_MIN_PRICE, title="Цена от", example="45000")
            return True
        if normalized == BUTTON_EDIT_MAX_PRICE.casefold():
            self._prompt_numeric_value(chat_id, ui_mode=UI_MODE_AWAIT_MAX_PRICE, title="Цена до", example="85000")
            return True
        if normalized == BUTTON_EDIT_AREA.casefold():
            self._prompt_numeric_value(chat_id, ui_mode=UI_MODE_AWAIT_MIN_AREA, title="Площадь от", example="60")
            return True
        if normalized == BUTTON_RESET_SETTINGS.casefold():
            self._set_ui_mode(chat_id, UI_MODE_CONFIRM_RESET)
            self.notifier.send_message(
                "<b>Сбросить настройки?</b>\nВерну текущие параметры к базовому профилю поиска.",
                chat_id=chat_id,
                reply_markup=self._reset_confirm_menu(),
            )
            return True

        return False

    def _toggle_district(self, chat_id: str, text: str) -> bool:
        district = parse_toggle_label(text)
        if district not in SUPPORTED_DISTRICTS:
            return False

        profile = self._load_profile()
        if district in profile.districts:
            if len(profile.districts) == 1:
                self._send_districts_menu(chat_id, prefix="Нужно оставить хотя бы один район.")
                return True
            updated = [item for item in profile.districts if item != district]
        else:
            updated = list(profile.districts) + [district]

        updated_profile = replace(profile, districts=self._sort_districts(updated))
        self._save_profile(updated_profile)
        self._send_districts_menu(chat_id, prefix="Районы обновил.")
        return True

    def _toggle_source(self, chat_id: str, text: str) -> bool:
        display_name = parse_toggle_label(text)
        source = next((item for item in self._all_sources() if item.display_name == display_name), None)
        if source is None:
            return False

        profile = self._load_profile()
        enabled = list(profile.enabled_source_keys)
        if source.source_key in enabled:
            if len(enabled) == 1:
                self._send_sources_menu(chat_id, prefix="Нужно оставить хотя бы один источник.")
                return True
            enabled = [item for item in enabled if item != source.source_key]
        else:
            enabled.append(source.source_key)

        updated_profile = replace(profile, enabled_source_keys=self._sort_enabled_sources(enabled))
        self._save_profile(updated_profile)
        self._send_sources_menu(chat_id, prefix="Источники обновил.")
        return True

    def _update_rooms(self, chat_id: str, text: str) -> bool:
        normalized = text.strip()
        raw_value = normalized.rstrip("+")
        if not raw_value.isdigit():
            return False
        min_rooms = int(raw_value)
        if min_rooms not in ROOM_OPTIONS:
            return False

        profile = self._load_profile()
        updated_profile = replace(profile, min_rooms=min_rooms)
        self._save_profile(updated_profile)
        self._send_settings_menu(chat_id, prefix="Минимум по комнатам обновил.")
        return True

    def _handle_numeric_input(self, chat_id: str, text: str, ui_mode: str) -> bool:
        normalized = text.strip()
        if normalized.casefold() == BUTTON_CLEAR_LIMIT.casefold():
            self._clear_numeric_limit(chat_id, ui_mode)
            return True

        digits = normalized.replace(" ", "").replace(",", ".")
        try:
            value = float(digits)
        except ValueError:
            self.notifier.send_message("Нужно прислать число. Например: <code>45000</code>", chat_id=chat_id, reply_markup=self._numeric_input_menu())
            return True

        if value <= 0:
            self.notifier.send_message("Число должно быть больше нуля.", chat_id=chat_id, reply_markup=self._numeric_input_menu())
            return True

        profile = self._load_profile()
        if ui_mode == UI_MODE_AWAIT_MIN_PRICE:
            new_value = int(value)
            if profile.max_price_rub is not None and new_value > profile.max_price_rub:
                self.notifier.send_message(
                    "Цена от не может быть больше цены до.",
                    chat_id=chat_id,
                    reply_markup=self._numeric_input_menu(),
                )
                return True
            updated_profile = replace(profile, min_price_rub=new_value)
            message = "Минимальную цену обновил."
        elif ui_mode == UI_MODE_AWAIT_MAX_PRICE:
            new_value = int(value)
            if profile.min_price_rub is not None and new_value < profile.min_price_rub:
                self.notifier.send_message(
                    "Цена до не может быть меньше цены от.",
                    chat_id=chat_id,
                    reply_markup=self._numeric_input_menu(),
                )
                return True
            updated_profile = replace(profile, max_price_rub=new_value)
            message = "Максимальную цену обновил."
        else:
            updated_profile = replace(profile, min_area_m2=float(value))
            message = "Минимальную площадь обновил."

        self._save_profile(updated_profile)
        self._send_settings_menu(chat_id, prefix=message)
        return True

    def _clear_numeric_limit(self, chat_id: str, ui_mode: str) -> None:
        profile = self._load_profile()
        if ui_mode == UI_MODE_AWAIT_MIN_PRICE:
            updated_profile = replace(profile, min_price_rub=None)
            message = "Минимальную цену убрал."
        elif ui_mode == UI_MODE_AWAIT_MAX_PRICE:
            updated_profile = replace(profile, max_price_rub=None)
            message = "Максимальную цену убрал."
        else:
            updated_profile = replace(profile, min_area_m2=None)
            message = "Минимальную площадь убрал."
        self._save_profile(updated_profile)
        self._send_settings_menu(chat_id, prefix=message)

    def _handle_reset_confirmation(self, chat_id: str, text: str) -> bool:
        normalized = text.strip().casefold()
        if normalized != BUTTON_CONFIRM_RESET.casefold():
            return False

        profile = default_search_profile(self.settings)
        self._save_profile(profile)
        self._send_settings_menu(chat_id, prefix="Вернул базовые настройки поиска.")
        return True

    def _load_profile(self) -> SearchProfile:
        return load_search_profile(self.settings, self.state)

    def _save_profile(self, profile: SearchProfile) -> None:
        save_search_profile(self.state, profile)

    def _load_offset(self) -> int | None:
        value = self.state.get_meta(UPDATE_OFFSET_KEY)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _main_menu(self) -> dict:
        rows = [[{"text": BUTTON_CHECK_ALL}], [{"text": BUTTON_RANDOM_PICK}]]
        for source in self._enabled_sources():
            rows.append([{"text": source.display_name}])
        rows.append([{"text": BUTTON_SETTINGS}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "persistent": True,
        }

    def _source_menu(self, source: SourceConfig) -> dict:
        return {
            "keyboard": [
                [{"text": self._check_source_button(source)}],
                [{"text": BUTTON_LAST}, {"text": BUTTON_LAST_THREE}],
                [{"text": BUTTON_MENU}, {"text": BUTTON_SETTINGS}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    @staticmethod
    def _settings_menu() -> dict:
        return {
            "keyboard": [
                [{"text": BUTTON_EDIT_DISTRICTS}],
                [{"text": BUTTON_EDIT_MIN_PRICE}, {"text": BUTTON_EDIT_MAX_PRICE}],
                [{"text": BUTTON_EDIT_ROOMS}, {"text": BUTTON_EDIT_AREA}],
                [{"text": BUTTON_EDIT_SOURCES}],
                [{"text": BUTTON_RESET_SETTINGS}],
                [{"text": BUTTON_MENU}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    def _districts_menu(self, profile: SearchProfile) -> dict:
        rows = []
        districts = set(profile.districts)
        items = [format_district_button(district, enabled=district in districts) for district in SUPPORTED_DISTRICTS]
        for index in range(0, len(items), 2):
            rows.append([{"text": item} for item in items[index : index + 2]])
        rows.append([{"text": BUTTON_SETTINGS_BACK}, {"text": BUTTON_MENU}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "persistent": True,
        }

    def _sources_menu(self, profile: SearchProfile) -> dict:
        rows = []
        enabled = set(profile.enabled_source_keys)
        labels = [format_source_button(source, enabled=source.source_key in enabled) for source in self._all_sources()]
        for label in labels:
            rows.append([{"text": label}])
        rows.append([{"text": BUTTON_SETTINGS_BACK}, {"text": BUTTON_MENU}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "persistent": True,
        }

    @staticmethod
    def _rooms_menu() -> dict:
        return {
            "keyboard": [
                [{"text": "3+"}, {"text": "4+"}, {"text": "5+"}],
                [{"text": BUTTON_SETTINGS_BACK}, {"text": BUTTON_MENU}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    @staticmethod
    def _numeric_input_menu(*, allow_clear: bool = True) -> dict:
        rows = []
        if allow_clear:
            rows.append([{"text": BUTTON_CLEAR_LIMIT}])
        rows.append([{"text": BUTTON_SETTINGS_BACK}, {"text": BUTTON_MENU}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "persistent": True,
        }

    @staticmethod
    def _reset_confirm_menu() -> dict:
        return {
            "keyboard": [
                [{"text": BUTTON_CONFIRM_RESET}],
                [{"text": BUTTON_SETTINGS_BACK}, {"text": BUTTON_MENU}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    def _enabled_sources(self) -> list[SourceConfig]:
        return [source for source in self._all_sources() if source.enabled]

    def _all_sources(self) -> list[SourceConfig]:
        profile = self._load_profile()
        return [apply_search_profile_to_source(source, profile) for source in self.settings.sources]

    def _match_source_by_text(self, text: str) -> SourceConfig | None:
        normalized = text.strip().casefold()
        for source in self._enabled_sources():
            if source.display_name.casefold() == normalized:
                return source
        return None

    def _get_selected_source(self, chat_id: str) -> SourceConfig | None:
        source_key = self.state.get_meta(self._chat_meta_key(chat_id, SELECTED_SOURCE_KEY))
        if not source_key:
            enabled_sources = self._enabled_sources()
            if len(enabled_sources) == 1:
                return enabled_sources[0]
            return None

        for source in self._enabled_sources():
            if source.source_key == source_key:
                return source
        return None

    def _set_selected_source(self, chat_id: str, source: SourceConfig) -> None:
        self.state.set_meta(self._chat_meta_key(chat_id, SELECTED_SOURCE_KEY), source.source_key)

    def _clear_selected_source(self, chat_id: str) -> None:
        self.state.set_meta(self._chat_meta_key(chat_id, SELECTED_SOURCE_KEY), "")

    def _get_ui_mode(self, chat_id: str) -> str:
        return self.state.get_meta(self._chat_meta_key(chat_id, UI_MODE_KEY)) or UI_MODE_NONE

    def _set_ui_mode(self, chat_id: str, value: str) -> None:
        self.state.set_meta(self._chat_meta_key(chat_id, UI_MODE_KEY), value)

    def _clear_chat_context(self, chat_id: str) -> None:
        self._clear_selected_source(chat_id)
        self._set_ui_mode(chat_id, UI_MODE_NONE)

    def _require_selected_source(self, chat_id: str, selected_source: SourceConfig | None) -> SourceConfig | None:
        if selected_source is not None:
            return selected_source

        self.notifier.send_message(
            "Сначала выбери источник, который хочешь проверить.",
            chat_id=chat_id,
            reply_markup=self._main_menu(),
        )
        return None

    @staticmethod
    def _check_source_button(source: SourceConfig) -> str:
        return f"Проверить {source.display_name}"

    def _check_button_aliases(self, selected_source: SourceConfig | None) -> set[str]:
        aliases = {BUTTON_CHECK_NOW.casefold(), "/check"}
        if selected_source is not None:
            aliases.add(self._check_source_button(selected_source).casefold())
        return aliases

    def _build_single_source_summary(self, source: SourceConfig, result: RunResult) -> str:
        lines = [f"<b>{source.display_name}</b>"]
        if result.bootstrap and result.bootstrap_reason == "profile_changed":
            lines.append("Настройки обновились, поэтому я заново запомнил текущие объявления.")
        elif result.bootstrap:
            lines.append("Я запомнил текущие объявления.")
        elif result.new_count > 0:
            lines.append(f"Нашёл новых объявлений: {result.new_count}.")
        else:
            lines.append("Новых объявлений нет.")

        lines.append(self._format_inventory_line(result))
        if result.new_count > 0:
            lines.append("Новые объявления уже отправил отдельными сообщениями.")
        elif result.bootstrap:
            lines.append("Дальше буду присылать только новые объявления.")
        return "\n".join(lines)

    def _build_all_sources_summary(self, results: list[RunResult]) -> str:
        lines = ["<b>Проверка завершена</b>"]
        total_new = 0
        had_bootstrap = False

        for result in results:
            source = self._get_source_by_key(result.source_key)
            lines.append(self._format_result_line(source, result))
            total_new += result.new_count
            had_bootstrap = had_bootstrap or result.bootstrap

        if total_new > 0:
            lines.append("Новые объявления уже отправил отдельными сообщениями.")
        elif had_bootstrap:
            lines.append("Теперь буду присылать только новые объявления.")

        return "\n".join(lines)

    def _format_result_line(self, source: SourceConfig, result: RunResult) -> str:
        if result.bootstrap and result.bootstrap_reason == "profile_changed":
            prefix = f"{source.display_name}: настройки обновились, текущую выдачу заново запомнил."
        elif result.bootstrap:
            prefix = f"{source.display_name}: запомнил текущие объявления."
        elif result.new_count > 0:
            prefix = f"{source.display_name}: новых объявлений {result.new_count}."
        else:
            prefix = f"{source.display_name}: новых объявлений нет."
        return f"{prefix} {self._format_inventory_line(result)}"

    @staticmethod
    def _format_inventory_line(result: RunResult) -> str:
        matched = result.matched_count if result.matched_count is not None else result.items_fetched
        scanned = result.scanned_count if result.scanned_count is not None else matched
        if scanned > matched:
            return f"Просмотрел {scanned}, подошло {matched}."
        return f"Подходящих сейчас: {matched}."

    def _get_source_by_key(self, source_key: str) -> SourceConfig:
        for source in self._all_sources():
            if source.source_key == source_key:
                return source
        raise ConfigError(f"Unknown source_key: {source_key}")

    def _resolve_allowed_chat_ids(self) -> set[str]:
        ids = self.settings.telegram.chat_ids or ([self.settings.telegram.chat_id] if self.settings.telegram.chat_id else [])
        return {str(item) for item in ids if str(item).strip()}

    def _remember_random_pick(self, listing: Listing, *, shown_at: str) -> None:
        existing = self.state.get_listing_row(listing.source_key, listing.external_id)
        if existing is None:
            self.state.insert_listing(listing, notification_state="suppressed", seen_at=shown_at)
        else:
            self.state.update_listing_seen(listing, seen_at=shown_at)
            self.state.suppress_listing_notification(listing.source_key, listing.external_id)

        self.state.mark_picker_seen(
            picker_key=RANDOM_PICKER_KEY,
            listing_key=listing.content_fingerprint,
            source_key=listing.source_key,
            external_id=listing.external_id,
            shown_at=shown_at,
        )

    def _handle_unauthorized_chat(self, *, chat_id: str) -> None:
        text = (
            "<b>HataBot</b>\n"
            "Этот чат пока не подключён.\n"
            f"Попроси добавить ID: <code>{chat_id}</code>\n"
            "После добавления нажми /start ещё раз."
        )
        try:
            self.notifier.send_message(text, chat_id=chat_id)
        except Exception:
            self.logger.exception("Failed to send unauthorized-chat hint to chat_id=%s", chat_id)

    @staticmethod
    def _sort_districts(districts: list[str]) -> list[str]:
        order = {name: index for index, name in enumerate(SUPPORTED_DISTRICTS)}
        return sorted(districts, key=lambda item: order.get(item, 999))

    def _sort_enabled_sources(self, source_keys: list[str]) -> list[str]:
        order = {source.source_key: index for index, source in enumerate(self.settings.sources)}
        return sorted(source_keys, key=lambda item: order.get(item, 999))

    @staticmethod
    def _chat_meta_key(chat_id: str, key: str) -> str:
        return f"{key}:{chat_id}"

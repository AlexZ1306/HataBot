from __future__ import annotations

import logging

from hata_bot.exceptions import ConfigError, SingleInstanceError
from hata_bot.locking import SingleInstanceLock
from hata_bot.models import Listing, RunResult, Settings, SourceConfig
from hata_bot.notifiers.telegram import TelegramNotifier
from hata_bot.services.monitor import MonitorService
from hata_bot.state import StateStore


BUTTON_CHECK_NOW = "Проверить сейчас"
BUTTON_LAST = "Последнее объявление"
BUTTON_LAST_THREE = "Последние 3 объявления"
BUTTON_MENU = "Меню"

UPDATE_OFFSET_KEY = "telegram_update_offset"
SELECTED_SOURCE_KEY = "telegram_selected_source"


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
        self.allowed_chat_id = str(settings.telegram.chat_id)
        self.listener_lock_file = settings.app.lock_file.with_name("hatabot-telegram-listener.lock")

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
        if chat_id != self.allowed_chat_id:
            self.logger.warning("Ignoring Telegram update from unauthorized chat_id=%s", chat_id)
            return

        self.handle_message(chat_id=chat_id, text=text)

    def handle_message(self, *, chat_id: str, text: str) -> None:
        normalized = text.strip().casefold()
        if normalized in {"/start", "/help", "/menu", BUTTON_MENU.casefold(), "start"}:
            self._clear_selected_source()
            self._send_main_menu(chat_id)
            return

        source = self._match_source_by_text(text)
        if source is not None:
            self._set_selected_source(source)
            self._send_source_menu(chat_id, source)
            return

        selected_source = self._get_selected_source()
        if normalized in {BUTTON_CHECK_NOW.casefold(), "/check"}:
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
            "Выбери источник и пользуйся кнопками ниже. Если захочешь сменить источник, нажми Меню.",
            chat_id=chat_id,
            reply_markup=reply_markup,
        )

    def _send_main_menu(self, chat_id: str) -> None:
        sources = self._enabled_sources()
        names = ", ".join(source.display_name for source in sources)
        text = (
            "<b>HataBot готов</b>\n"
            f"Выбери источник: <b>{names}</b>.\n"
            "После выбора появятся кнопки для мгновенной проверки и просмотра свежих объявлений."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._main_menu())

    def _send_source_menu(self, chat_id: str, source: SourceConfig) -> None:
        text = (
            f"<b>Источник: {source.display_name}</b>\n"
            "Кнопка <b>Проверить сейчас</b> запускает ручную проверку только для этого источника.\n"
            "Кнопки <b>Последнее объявление</b> и <b>Последние 3 объявления</b> показывают самые свежие карточки из текущей выдачи.\n"
            "Кнопка <b>Меню</b> возвращает к выбору источника."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._source_menu(source))

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

        if result.bootstrap:
            text = (
                f"Первая инициализация для {source.display_name} завершена.\n"
                f"Я запомнил текущую выдачу: {result.items_fetched} объявлений.\n"
                "Следующие проверки будут присылать только новые варианты."
            )
        elif result.new_count == 0:
            text = (
                f"Проверка {source.display_name} завершена.\n"
                "Новых объявлений нет.\n"
                f"Сейчас в просмотренной выдаче: {result.items_fetched} карточек."
            )
        else:
            text = (
                f"Проверка {source.display_name} завершена.\n"
                f"Найдено новых объявлений: {result.new_count}.\n"
                "Я уже отправил их отдельными сообщениями."
            )

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
                listings[0],
                poll_note=source.poll_note or "Самое свежее объявление",
                chat_id=chat_id,
            )
        else:
            self.notifier.send_message(
                f"Показываю последние {limit} объявления из {source.display_name} по одному сообщению.",
                chat_id=chat_id,
                reply_markup=self._source_menu(source),
            )
            for index, listing in enumerate(listings, start=1):
                self.notifier.send_listing(
                    listing,
                    poll_note=f"{source.poll_note or 'Последние объявления'} [{index}/{limit}]",
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

    def _build_provider(self, source: SourceConfig):
        if self.provider_factory is not None:
            return self.provider_factory(source)

        from hata_bot.providers.avito import AvitoProvider
        from hata_bot.providers.cian import CianProvider

        if source.provider == "avito":
            return AvitoProvider(source)
        if source.provider == "cian":
            return CianProvider(source, data_dir=self.settings.app.data_dir)
        raise ConfigError(f"Unsupported provider: {source.provider}")

    def _load_offset(self) -> int | None:
        value = self.state.get_meta(UPDATE_OFFSET_KEY)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _main_menu(self) -> dict:
        rows = [[{"text": source.display_name}] for source in self._enabled_sources()]
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "persistent": True,
        }

    def _source_menu(self, source: SourceConfig) -> dict:
        return {
            "keyboard": [
                [{"text": BUTTON_CHECK_NOW}],
                [{"text": BUTTON_LAST}, {"text": BUTTON_LAST_THREE}],
                [{"text": BUTTON_MENU}],
            ],
            "resize_keyboard": True,
            "persistent": True,
        }

    def _enabled_sources(self) -> list[SourceConfig]:
        sources = [source for source in self.settings.sources if source.enabled]
        if not sources:
            raise ConfigError("No enabled sources configured.")
        return sources

    def _match_source_by_text(self, text: str) -> SourceConfig | None:
        normalized = text.strip().casefold()
        for source in self._enabled_sources():
            if source.display_name.casefold() == normalized:
                return source
        return None

    def _get_selected_source(self) -> SourceConfig | None:
        source_key = self.state.get_meta(SELECTED_SOURCE_KEY)
        if not source_key:
            enabled_sources = self._enabled_sources()
            if len(enabled_sources) == 1:
                return enabled_sources[0]
            return None

        for source in self._enabled_sources():
            if source.source_key == source_key:
                return source
        return None

    def _set_selected_source(self, source: SourceConfig) -> None:
        self.state.set_meta(SELECTED_SOURCE_KEY, source.source_key)

    def _clear_selected_source(self) -> None:
        self.state.set_meta(SELECTED_SOURCE_KEY, "")

    def _require_selected_source(self, chat_id: str, selected_source: SourceConfig | None) -> SourceConfig | None:
        if selected_source is not None:
            return selected_source

        self.notifier.send_message(
            "Сначала выбери источник, который хочешь проверить.",
            chat_id=chat_id,
            reply_markup=self._main_menu(),
        )
        return None

from __future__ import annotations

import logging

from hata_bot.config import get_source
from hata_bot.exceptions import ConfigError, SingleInstanceError
from hata_bot.locking import SingleInstanceLock
from hata_bot.models import Listing, RunResult, Settings, SourceConfig
from hata_bot.notifiers.telegram import (
    TelegramNotifier,
    build_new_listing_message,
)
from hata_bot.services.monitor import MonitorService
from hata_bot.state import StateStore


BUTTON_CHECK_NOW = "Проверить сейчас"
BUTTON_LAST = "Последнее объявление"
BUTTON_LAST_THREE = "Последние 3 объявления"

UPDATE_OFFSET_KEY = "telegram_update_offset"

MAIN_MENU = {
    "keyboard": [
        [{"text": BUTTON_CHECK_NOW}],
        [{"text": BUTTON_LAST}, {"text": BUTTON_LAST_THREE}],
    ],
    "resize_keyboard": True,
    "persistent": True,
}


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
        if normalized in {"/start", "/help", "/menu", "меню", "start"}:
            self._send_menu(chat_id)
            return
        if normalized in {BUTTON_CHECK_NOW.casefold(), "/check"}:
            self._handle_check_now(chat_id)
            return
        if normalized in {BUTTON_LAST.casefold(), "/latest"}:
            self._handle_latest(chat_id, limit=1)
            return
        if normalized in {BUTTON_LAST_THREE.casefold(), "/latest3", "/latest_3"}:
            self._handle_latest(chat_id, limit=3)
            return

        self.notifier.send_message(
            "Я понимаю кнопки ниже: проверить сейчас, последнее объявление или последние 3 объявления.",
            chat_id=chat_id,
            reply_markup=MAIN_MENU,
        )

    def _send_menu(self, chat_id: str) -> None:
        text = (
            "<b>HataBot готов</b>\n"
            "Кнопка <b>Проверить сейчас</b> запускает мгновенную проверку Авито.\n"
            "Кнопки <b>Последнее объявление</b> и <b>Последние 3 объявления</b> показывают самые свежие карточки из текущей выдачи."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=MAIN_MENU)

    def _handle_check_now(self, chat_id: str) -> None:
        try:
            results = self.run_monitor_now()
        except SingleInstanceError:
            self.notifier.send_message("Проверка уже идёт прямо сейчас. Попробуй ещё раз через минуту.", chat_id=chat_id)
            return
        except Exception as exc:
            self.notifier.send_message(f"Не получилось проверить выдачу: {exc}", chat_id=chat_id)
            return

        total_items = sum(item.items_fetched for item in results)
        total_new = sum(item.new_count for item in results)
        bootstrap = any(item.bootstrap for item in results)

        if bootstrap:
            text = (
                "Первая инициализация завершена.\n"
                f"Я запомнил текущую выдачу: {total_items} объявлений.\n"
                "Следующие проверки будут присылать только новые варианты."
            )
        elif total_new == 0:
            text = (
                "Проверка завершена.\n"
                "Новых объявлений нет.\n"
                f"Сейчас в просмотренной выдаче: {total_items} карточек."
            )
        else:
            text = (
                "Проверка завершена.\n"
                f"Найдено новых объявлений: {total_new}.\n"
                "Я уже отправил их отдельными сообщениями."
            )

        self.notifier.send_message(text, chat_id=chat_id, reply_markup=MAIN_MENU)

    def _handle_latest(self, chat_id: str, *, limit: int) -> None:
        try:
            source, listings = self.fetch_latest_listings(limit=limit)
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
                f"Показываю последние {limit} объявления по одному сообщению.",
                chat_id=chat_id,
                reply_markup=MAIN_MENU,
            )
            for index, listing in enumerate(listings, start=1):
                self.notifier.send_listing(
                    listing,
                    poll_note=f"{source.poll_note or 'Последние объявления'} [{index}/{limit}]",
                    chat_id=chat_id,
                )

    def run_monitor_now(self) -> list[RunResult]:
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
                return service.run()
            finally:
                monitor_state.close()

    def fetch_latest_listings(self, *, limit: int) -> tuple[SourceConfig, list[Listing]]:
        source = self._default_source()
        provider = self._build_provider(source)
        listings = provider.fetch()
        return source, listings[:limit]

    def _default_source(self) -> SourceConfig:
        for source in self.settings.sources:
            if source.enabled:
                return source
        raise ConfigError("No enabled sources configured.")

    def _build_provider(self, source: SourceConfig):
        if self.provider_factory is not None:
            return self.provider_factory(source)

        from hata_bot.providers.avito import AvitoProvider

        if source.provider == "avito":
            return AvitoProvider(source)
        raise ConfigError(f"Unsupported provider: {source.provider}")

    def _load_offset(self) -> int | None:
        value = self.state.get_meta(UPDATE_OFFSET_KEY)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

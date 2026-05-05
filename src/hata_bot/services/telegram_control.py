from __future__ import annotations

import logging

from hata_bot.exceptions import ConfigError, SingleInstanceError
from hata_bot.locking import SingleInstanceLock
from hata_bot.models import Listing, RunResult, Settings, SourceConfig
from hata_bot.notifiers.telegram import TelegramNotifier
from hata_bot.services.monitor import MonitorService
from hata_bot.state import StateStore


BUTTON_CHECK_ALL = "Проверить новые объявления"
BUTTON_CHECK_NOW = "Проверить выбранный сервис"
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
        self.allowed_chat_ids = self._resolve_allowed_chat_ids()
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
        if chat_id not in self.allowed_chat_ids:
            self.logger.warning("Received Telegram update from unauthorized chat_id=%s", chat_id)
            self._handle_unauthorized_chat(chat_id=chat_id)
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
        if normalized in {BUTTON_CHECK_ALL.casefold(), "/check_all"}:
            self._clear_selected_source()
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
        text = (
            "<b>HataBot</b>\n"
            "Можно сразу проверить все сервисы или открыть один сервис отдельно."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._main_menu())

    def _send_source_menu(self, chat_id: str, source: SourceConfig) -> None:
        text = (
            f"<b>{source.display_name}</b>\n"
            "Можно проверить новые объявления или посмотреть самые свежие варианты."
        )
        self.notifier.send_message(text, chat_id=chat_id, reply_markup=self._source_menu(source))

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
                listings[0],
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
                    listing,
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

    def _load_offset(self) -> int | None:
        value = self.state.get_meta(UPDATE_OFFSET_KEY)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _main_menu(self) -> dict:
        rows = [[{"text": BUTTON_CHECK_ALL}]]
        for source in self._enabled_sources():
            rows.append([{"text": source.display_name}])
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
        if result.bootstrap:
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
        if result.bootstrap:
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
        for source in self._enabled_sources():
            if source.source_key == source_key:
                return source
        raise ConfigError(f"Unknown source_key: {source_key}")

    def _resolve_allowed_chat_ids(self) -> set[str]:
        ids = self.settings.telegram.chat_ids or ([self.settings.telegram.chat_id] if self.settings.telegram.chat_id else [])
        return {str(item) for item in ids if str(item).strip()}

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

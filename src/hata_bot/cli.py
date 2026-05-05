from __future__ import annotations

import argparse
import logging
from pathlib import Path

from hata_bot.config import ensure_directories, get_source, load_settings
from hata_bot.exceptions import ConfigError, HataBotError
from hata_bot.locking import SingleInstanceLock
from hata_bot.logging_utils import setup_logging
from hata_bot.notifiers.telegram import TelegramNotifier
from hata_bot.services.monitor import MonitorService
from hata_bot.services.telegram_control import TelegramControlBot
from hata_bot.state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hatabot", description="Local apartment listing monitor.")
    parser.add_argument("--config", default="config/config.yaml", help="Path to YAML config file.")
    parser.add_argument("--env-file", default=None, help="Optional path to .env file.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one monitoring pass.")
    run_parser.add_argument("--source", default=None, help="Run only a specific source_key.")

    subparsers.add_parser("doctor", help="Validate config, state storage, and Telegram connectivity.")

    test_parser = subparsers.add_parser("test-telegram", help="Send a test Telegram message.")
    test_parser.add_argument("--message", default="Telegram integration looks healthy.", help="Message body.")

    bot_parser = subparsers.add_parser("telegram-bot", help="Run Telegram control bot with buttons.")
    bot_parser.add_argument("--poll-timeout", type=int, default=25, help="Telegram long-poll timeout in seconds.")
    bot_parser.add_argument("--once", action="store_true", help="Process currently queued updates once and exit.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(config_path=args.config, env_path=args.env_file)
        ensure_directories(settings)
        logger = setup_logging(settings.app.log_dir / "hatabot.log", verbose=args.verbose)

        if args.command == "doctor":
            return _doctor(settings, logger)
        if args.command == "test-telegram":
            return _test_telegram(settings, logger, args.message)
        if args.command == "run":
            return _run(settings, logger, source_key=args.source)
        if args.command == "telegram-bot":
            return _telegram_bot(settings, logger, poll_timeout=args.poll_timeout, once=args.once)
    except HataBotError as exc:
        logging.getLogger("hata_bot").error("%s", exc)
        return 1
    except Exception:
        logging.getLogger("hata_bot").exception("Unexpected unhandled error")
        return 1

    parser.print_help()
    return 1


def _doctor(settings, logger: logging.Logger) -> int:
    state = StateStore(settings.app.database_file)
    state.initialize()
    state.close()

    logger.info("Config loaded from %s", settings.app.project_root)
    for source in settings.sources:
        if source.enabled:
            logger.info("Enabled source: %s (%s)", source.source_key, source.provider)

    notifier = TelegramNotifier(settings.telegram)
    bot_username = notifier.healthcheck()
    logger.info("Telegram connectivity OK. Bot username: %s", bot_username)
    return 0


def _test_telegram(settings, logger: logging.Logger, message: str) -> int:
    notifier = TelegramNotifier(settings.telegram)
    notifier.send_test_message(message)
    logger.info("Telegram test message sent successfully.")
    return 0


def _run(settings, logger: logging.Logger, *, source_key: str | None) -> int:
    if source_key:
        get_source(settings, source_key)

    notifier = TelegramNotifier(settings.telegram)

    with SingleInstanceLock(settings.app.lock_file):
        state = StateStore(settings.app.database_file)
        state.initialize()
        try:
            service = MonitorService(settings, state, notifier, logger=logger)
            results = service.run(source_key=source_key)
        finally:
            state.close()

    for result in results:
        suffix = ""
        if result.scanned_count is not None and result.matched_count is not None:
            suffix = f" scanned_count={result.scanned_count} matched_count={result.matched_count}"
        if result.pages_checked is not None:
            suffix += f" pages_checked={result.pages_checked}"
        logger.info(
            "Source %s finished with status=%s items_fetched=%s new_count=%s bootstrap=%s%s",
            result.source_key,
            result.status,
            result.items_fetched,
            result.new_count,
            result.bootstrap,
            suffix,
        )
    return 0


def _telegram_bot(settings, logger: logging.Logger, *, poll_timeout: int, once: bool) -> int:
    notifier = TelegramNotifier(settings.telegram)
    state = StateStore(settings.app.database_file)
    state.initialize()

    listener_lock_file = settings.app.lock_file.with_name("hatabot-telegram-listener.lock")
    with SingleInstanceLock(listener_lock_file):
        try:
            bot = TelegramControlBot(settings, notifier, state, logger=logger)
            logger.info("Telegram control bot listener started.")
            bot.poll_forever(poll_timeout=poll_timeout, once=once)
        finally:
            state.close()

    return 0

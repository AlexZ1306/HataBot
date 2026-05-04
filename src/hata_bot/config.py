from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from hata_bot.exceptions import ConfigError
from hata_bot.models import AppConfig, Settings, SourceConfig, TelegramConfig


def load_settings(config_path: str | Path = "config/config.yaml", env_path: str | Path | None = None) -> Settings:
    config_file = Path(config_path).resolve()
    if not config_file.exists():
        raise ConfigError(f"Configuration file not found: {config_file}")

    project_root = _infer_project_root(config_file)
    dotenv_path = Path(env_path).resolve() if env_path else project_root / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    app_cfg = raw.get("app", {})
    notifications_cfg = raw.get("notifications", {})
    telegram_cfg = notifications_cfg.get("telegram", {})
    raw_sources = raw.get("sources", [])

    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("At least one source must be configured in sources[].")

    data_dir = _resolve_path(project_root, app_cfg.get("data_dir", "data"))
    log_dir = _resolve_path(project_root, app_cfg.get("log_dir", "logs"))
    database_file = _resolve_path(project_root, app_cfg.get("database_file", "data/hata.db"))
    lock_file = _resolve_path(project_root, app_cfg.get("lock_file", "data/hatabot.lock"))

    app = AppConfig(
        project_root=project_root,
        data_dir=data_dir,
        log_dir=log_dir,
        database_file=database_file,
        lock_file=lock_file,
    )

    telegram = TelegramConfig(
        enabled=bool(telegram_cfg.get("enabled", True)),
        bot_token=_read_env("HATABOT_TELEGRAM_BOT_TOKEN"),
        chat_id=_read_env("HATABOT_TELEGRAM_CHAT_ID"),
    )

    sources = [_parse_source(item) for item in raw_sources]
    if not any(source.enabled for source in sources):
        raise ConfigError("At least one source must be enabled.")

    return Settings(app=app, telegram=telegram, sources=sources)


def ensure_directories(settings: Settings) -> None:
    settings.app.data_dir.mkdir(parents=True, exist_ok=True)
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)
    settings.app.database_file.parent.mkdir(parents=True, exist_ok=True)
    settings.app.lock_file.parent.mkdir(parents=True, exist_ok=True)


def get_source(settings: Settings, source_key: str) -> SourceConfig:
    for source in settings.sources:
        if source.source_key == source_key:
            return source
    raise ConfigError(f"Unknown source_key: {source_key}")


def _parse_source(raw_source: dict) -> SourceConfig:
    try:
        source_key = str(raw_source["source_key"]).strip()
        display_name = str(raw_source.get("display_name") or source_key).strip()
        provider = str(raw_source["provider"]).strip().lower()
        search_url = str(raw_source["search_url"]).strip()
    except KeyError as exc:
        raise ConfigError(f"Missing required source field: {exc.args[0]}") from exc

    if not source_key:
        raise ConfigError("source_key cannot be empty.")
    if not display_name:
        raise ConfigError(f"display_name cannot be empty for source {source_key}.")
    if provider not in {"avito", "cian"}:
        raise ConfigError(f"Unsupported provider '{provider}' for source {source_key}.")
    if not _looks_like_url(search_url):
        raise ConfigError(f"Invalid search_url for source {source_key}: {search_url}")

    max_pages = int(raw_source.get("max_pages", 1))
    timeout = int(raw_source.get("request_timeout_sec", 20))
    repost_days = int(raw_source.get("repost_suppression_days", 30))
    user_agent = str(raw_source.get("user_agent", "")).strip()

    if max_pages < 1:
        raise ConfigError(f"max_pages must be >= 1 for source {source_key}.")
    if timeout < 5:
        raise ConfigError(f"request_timeout_sec must be >= 5 for source {source_key}.")
    if repost_days < 1:
        raise ConfigError(f"repost_suppression_days must be >= 1 for source {source_key}.")
    if not user_agent:
        raise ConfigError(f"user_agent cannot be empty for source {source_key}.")

    required_districts = raw_source.get("required_districts", [])
    if required_districts is None:
        required_districts = []
    if not isinstance(required_districts, list):
        raise ConfigError(f"required_districts must be a list for source {source_key}.")

    exclude_text_patterns = raw_source.get("exclude_text_patterns", [])
    if exclude_text_patterns is None:
        exclude_text_patterns = []
    if not isinstance(exclude_text_patterns, list):
        raise ConfigError(f"exclude_text_patterns must be a list for source {source_key}.")

    sort_override = raw_source.get("sort_override")
    if sort_override is not None:
        sort_override = str(sort_override).strip() or None

    return SourceConfig(
        source_key=source_key,
        display_name=display_name,
        provider=provider,
        enabled=bool(raw_source.get("enabled", True)),
        search_url=search_url,
        max_pages=max_pages,
        request_timeout_sec=timeout,
        repost_suppression_days=repost_days,
        user_agent=user_agent,
        poll_note=str(raw_source.get("poll_note")).strip() if raw_source.get("poll_note") else None,
        required_districts=[str(item).strip() for item in required_districts if str(item).strip()],
        exclude_text_patterns=[str(item).strip() for item in exclude_text_patterns if str(item).strip()],
        sort_override=sort_override,
    )


def _read_env(name: str) -> str | None:
    import os

    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_path(project_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def _infer_project_root(config_file: Path) -> Path:
    if config_file.parent.name == "config":
        return config_file.parent.parent.resolve()
    return config_file.parent.resolve()


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

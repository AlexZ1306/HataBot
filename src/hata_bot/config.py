from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

from hata_bot.exceptions import ConfigError
from hata_bot.models import AppConfig, SchoolCommuteConfig, Settings, SourceConfig, TelegramConfig


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
    school_commute_cfg = raw.get("school_commute", {})
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
        chat_id=_resolve_primary_chat_id(),
        chat_ids=_resolve_chat_ids(),
    )
    school_commute = _parse_school_commute_config(school_commute_cfg)

    sources = [_parse_source(item) for item in raw_sources]
    if not any(source.enabled for source in sources):
        raise ConfigError("At least one source must be enabled.")

    return Settings(app=app, telegram=telegram, sources=sources, school_commute=school_commute)


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
    if provider not in {"avito", "cian", "domclick"}:
        raise ConfigError(f"Unsupported provider '{provider}' for source {source_key}.")
    if not _looks_like_url(search_url):
        raise ConfigError(f"Invalid search_url for source {source_key}: {search_url}")

    max_pages = int(raw_source.get("max_pages", 1))
    timeout = int(raw_source.get("request_timeout_sec", 20))
    repost_days = int(raw_source.get("repost_suppression_days", 30))
    user_agent = str(raw_source.get("user_agent", "")).strip()
    min_price_rub = _optional_int(raw_source.get("min_price_rub"))
    max_price_rub = _optional_int(raw_source.get("max_price_rub"))
    min_area_m2 = _optional_float(raw_source.get("min_area_m2"))
    min_rooms = _optional_int(raw_source.get("min_rooms"))

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
        min_price_rub=min_price_rub,
        max_price_rub=max_price_rub,
        min_area_m2=min_area_m2,
        min_rooms=min_rooms,
        required_districts=[str(item).strip() for item in required_districts if str(item).strip()],
        exclude_text_patterns=[str(item).strip() for item in exclude_text_patterns if str(item).strip()],
        sort_override=sort_override,
    )


def _parse_school_commute_config(raw_school_commute: dict) -> SchoolCommuteConfig:
    if raw_school_commute is None:
        raw_school_commute = {}
    if not isinstance(raw_school_commute, dict):
        raise ConfigError("school_commute must be a mapping.")

    config = SchoolCommuteConfig(
        enabled=bool(raw_school_commute.get("enabled", False)),
        api_key=_read_env("HATABOT_2GIS_API_KEY"),
        origin_city_name=_optional_str(raw_school_commute.get("origin_city_name")),
        destination_name=_optional_str(raw_school_commute.get("destination_name")) or "Школа",
        destination_address=_optional_str(raw_school_commute.get("destination_address")),
        destination_lat=_optional_float(raw_school_commute.get("destination_lat")),
        destination_lon=_optional_float(raw_school_commute.get("destination_lon")),
        departure_time_local=_optional_str(raw_school_commute.get("departure_time_local")) or "07:30",
        cache_ttl_hours=int(raw_school_commute.get("cache_ttl_hours", 168)),
        request_timeout_sec=int(raw_school_commute.get("request_timeout_sec", 12)),
    )

    if config.cache_ttl_hours < 1:
        raise ConfigError("school_commute.cache_ttl_hours must be >= 1.")
    if config.request_timeout_sec < 5:
        raise ConfigError("school_commute.request_timeout_sec must be >= 5.")
    if not _looks_like_hhmm(config.departure_time_local):
        raise ConfigError("school_commute.departure_time_local must look like HH:MM.")
    if config.enabled and (config.destination_lat is None or config.destination_lon is None):
        raise ConfigError("school_commute.destination_lat and destination_lon are required when school_commute is enabled.")

    return config


def _read_env(name: str) -> str | None:
    import os

    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_env_list(name: str) -> list[str]:
    value = _read_env(name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _resolve_chat_ids() -> list[str]:
    explicit = _read_env_list("HATABOT_TELEGRAM_CHAT_IDS")
    if explicit:
        return explicit
    return _read_env_list("HATABOT_TELEGRAM_CHAT_ID")


def _resolve_primary_chat_id() -> str | None:
    explicit = _read_env_list("HATABOT_TELEGRAM_CHAT_IDS")
    if explicit:
        return explicit[0]
    fallback = _read_env_list("HATABOT_TELEGRAM_CHAT_ID")
    if fallback:
        return fallback[0]
    return None


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _optional_str(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
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


def _looks_like_hhmm(value: str) -> bool:
    parts = value.split(":", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return False
    hours = int(parts[0])
    minutes = int(parts[1])
    return 0 <= hours <= 23 and 0 <= minutes <= 59

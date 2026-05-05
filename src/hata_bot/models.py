from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Listing:
    source_key: str
    external_id: str
    url: str
    title: str
    price_rub: int | None
    rooms: int | None
    area_m2: float | None
    address: str | None
    metro: str | None
    published_text: str | None
    content_fingerprint: str
    image_url: str | None = None
    photo_urls: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceConfig:
    source_key: str
    display_name: str
    provider: str
    enabled: bool
    search_url: str
    max_pages: int
    request_timeout_sec: int
    repost_suppression_days: int
    user_agent: str
    poll_note: str | None = None
    min_price_rub: int | None = None
    max_price_rub: int | None = None
    min_area_m2: float | None = None
    min_rooms: int | None = None
    required_districts: list[str] = field(default_factory=list)
    exclude_text_patterns: list[str] = field(default_factory=list)
    sort_override: str | None = None


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool
    bot_token: str | None
    chat_id: str | None
    chat_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    data_dir: Path
    log_dir: Path
    database_file: Path
    lock_file: Path


@dataclass(slots=True)
class Settings:
    app: AppConfig
    telegram: TelegramConfig
    sources: list[SourceConfig]


@dataclass(slots=True)
class ProviderFetchStats:
    scanned_count: int
    matched_count: int
    pages_checked: int


@dataclass(slots=True)
class RunResult:
    source_key: str
    status: str
    items_fetched: int
    new_count: int
    bootstrap: bool
    error_message: str | None = None
    scanned_count: int | None = None
    matched_count: int | None = None
    pages_checked: int | None = None

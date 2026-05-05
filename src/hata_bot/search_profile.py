from __future__ import annotations

import json
from dataclasses import replace

from hata_bot.fingerprints import format_area, format_price
from hata_bot.models import SearchProfile, Settings, SourceConfig
from hata_bot.state import StateStore


SEARCH_PROFILE_META_KEY = "search_profile_v1"
SUPPORTED_CITY_NAME = "Новосибирск"
SUPPORTED_DISTRICTS = (
    "Дзержинский",
    "Железнодорожный",
    "Заельцовский",
    "Калининский",
    "Кировский",
    "Ленинский",
    "Октябрьский",
    "Первомайский",
    "Советский",
    "Центральный",
)
ROOM_OPTIONS = (3, 4, 5)

AVITO_DISTRICT_IDS = {
    "Дзержинский": "803",
    "Железнодорожный": "804",
    "Заельцовский": "805",
    "Калининский": "806",
    "Кировский": "807",
    "Ленинский": "808",
    "Октябрьский": "809",
    "Первомайский": "810",
    "Советский": "811",
    "Центральный": "812",
}


def default_search_profile(settings: Settings) -> SearchProfile:
    districts = _extract_default_districts(settings)
    min_price_rub = _first_not_none(source.min_price_rub for source in settings.sources)
    max_price_rub = _first_not_none(source.max_price_rub for source in settings.sources)
    min_area_m2 = _first_not_none(source.min_area_m2 for source in settings.sources)
    min_rooms = _first_not_none(source.min_rooms for source in settings.sources)
    enabled_source_keys = [source.source_key for source in settings.sources if source.enabled]

    return SearchProfile(
        city_name=SUPPORTED_CITY_NAME,
        districts=districts,
        min_price_rub=min_price_rub,
        max_price_rub=max_price_rub,
        min_area_m2=min_area_m2,
        min_rooms=min_rooms,
        enabled_source_keys=enabled_source_keys,
    )


def load_search_profile(settings: Settings, state: StateStore) -> SearchProfile:
    default = default_search_profile(settings)
    raw_value = state.get_meta(SEARCH_PROFILE_META_KEY)
    if not raw_value:
        return default

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return default

    if not isinstance(payload, dict):
        return default

    districts = _normalize_districts(payload.get("districts"), fallback=default.districts)
    enabled_source_keys = _normalize_enabled_sources(
        payload.get("enabled_source_keys"),
        source_keys=[source.source_key for source in settings.sources],
        fallback=default.enabled_source_keys,
    )

    return SearchProfile(
        city_name=SUPPORTED_CITY_NAME,
        districts=districts,
        min_price_rub=_optional_int(payload.get("min_price_rub"), fallback=default.min_price_rub),
        max_price_rub=_optional_int(payload.get("max_price_rub"), fallback=default.max_price_rub),
        min_area_m2=_optional_float(payload.get("min_area_m2"), fallback=default.min_area_m2),
        min_rooms=_optional_int(payload.get("min_rooms"), fallback=default.min_rooms),
        enabled_source_keys=enabled_source_keys,
    )


def save_search_profile(state: StateStore, profile: SearchProfile) -> None:
    payload = {
        "city_name": profile.city_name,
        "districts": list(profile.districts),
        "min_price_rub": profile.min_price_rub,
        "max_price_rub": profile.max_price_rub,
        "min_area_m2": profile.min_area_m2,
        "min_rooms": profile.min_rooms,
        "enabled_source_keys": list(profile.enabled_source_keys),
    }
    state.set_meta(SEARCH_PROFILE_META_KEY, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def apply_search_profile_to_source(source: SourceConfig, profile: SearchProfile) -> SourceConfig:
    districts = [district for district in profile.districts if district in SUPPORTED_DISTRICTS]
    if not districts:
        districts = list(SUPPORTED_DISTRICTS)

    return replace(
        source,
        enabled=source.enabled and source.source_key in profile.enabled_source_keys,
        min_price_rub=profile.min_price_rub,
        max_price_rub=profile.max_price_rub,
        min_area_m2=profile.min_area_m2,
        min_rooms=profile.min_rooms,
        required_districts=districts,
    )


def build_source_profile_signature(source: SourceConfig) -> str:
    payload = {
        "source_key": source.source_key,
        "provider": source.provider,
        "search_url": source.search_url,
        "enabled": source.enabled,
        "min_price_rub": source.min_price_rub,
        "max_price_rub": source.max_price_rub,
        "min_area_m2": source.min_area_m2,
        "min_rooms": source.min_rooms,
        "required_districts": list(source.required_districts),
        "exclude_text_patterns": list(source.exclude_text_patterns),
        "sort_override": source.sort_override,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def format_search_profile_summary(profile: SearchProfile, sources: list[SourceConfig]) -> str:
    enabled_names = [source.display_name for source in sources if source.source_key in profile.enabled_source_keys]
    lines = [
        "<b>Настройки поиска</b>",
        f"Город: {profile.city_name}",
        f"Районы: {', '.join(profile.districts)}",
        f"Цена: {_format_range(profile.min_price_rub, profile.max_price_rub)}",
        f"Комнаты: {_format_min_rooms(profile.min_rooms)}",
        f"Площадь: {_format_min_area(profile.min_area_m2)}",
        f"Источники: {', '.join(enabled_names) if enabled_names else 'не выбраны'}",
        "",
        "Изменения применяются сразу.",
        "После смены параметров я заново запомню текущую выдачу и не пришлю старые объявления.",
    ]
    return "\n".join(lines)


def format_district_button(district: str, *, enabled: bool) -> str:
    return f"{'✓' if enabled else '○'} {district}"


def format_source_button(source: SourceConfig, *, enabled: bool) -> str:
    return f"{'✓' if enabled else '○'} {source.display_name}"


def parse_toggle_label(text: str) -> str:
    normalized = text.strip()
    if normalized.startswith("✓ ") or normalized.startswith("○ "):
        return normalized[2:].strip()
    return normalized


def _extract_default_districts(settings: Settings) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for source in settings.sources:
        for district in source.required_districts:
            if district not in SUPPORTED_DISTRICTS or district in seen:
                continue
            seen.add(district)
            ordered.append(district)
    if ordered:
        return ordered
    return list(SUPPORTED_DISTRICTS)


def _normalize_districts(value, *, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    normalized = [str(item).strip() for item in value if str(item).strip() in SUPPORTED_DISTRICTS]
    return normalized or list(fallback)


def _normalize_enabled_sources(value, *, source_keys: list[str], fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    normalized = [str(item).strip() for item in value if str(item).strip() in source_keys]
    return normalized or list(fallback)


def _optional_int(value, *, fallback: int | None) -> int | None:
    if value in {None, ""}:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _optional_float(value, *, fallback: float | None) -> float | None:
    if value in {None, ""}:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _first_not_none(values):
    for value in values:
        if value is not None:
            return value
    return None


def _format_range(min_value: int | None, max_value: int | None) -> str:
    if min_value is not None and max_value is not None:
        return f"от {format_price(min_value)} до {format_price(max_value)}"
    if min_value is not None:
        return f"от {format_price(min_value)}"
    if max_value is not None:
        return f"до {format_price(max_value)}"
    return "не задана"


def _format_min_rooms(min_rooms: int | None) -> str:
    if min_rooms is None:
        return "не заданы"
    return f"от {min_rooms}"


def _format_min_area(min_area_m2: float | None) -> str:
    if min_area_m2 is None:
        return "не задана"
    return f"от {format_area(min_area_m2)} м²"

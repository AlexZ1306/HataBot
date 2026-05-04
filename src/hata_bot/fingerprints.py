from __future__ import annotations

import hashlib
import re


_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w\d]+", re.UNICODE)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return _SPACE_RE.sub(" ", value.strip())


def normalize_for_fingerprint(value: str | None) -> str:
    normalized = normalize_text(value).lower()
    return _NON_WORD_RE.sub("", normalized)


def build_content_fingerprint(
    *,
    source_key: str,
    title: str,
    price_rub: int | None,
    rooms: int | None,
    area_m2: float | None,
    address: str | None,
) -> str:
    parts = [
        normalize_for_fingerprint(source_key),
        normalize_for_fingerprint(title),
        str(price_rub or ""),
        str(rooms or ""),
        f"{area_m2:.1f}" if area_m2 is not None else "",
        normalize_for_fingerprint(address),
    ]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()


def parse_rooms_and_area(title: str) -> tuple[int | None, float | None]:
    rooms_match = re.search(r"(\d+)-к\.", title)
    area_match = re.search(r"квартира,\s*([\d,]+)\s*м²", title, re.IGNORECASE)

    rooms = int(rooms_match.group(1)) if rooms_match else None
    area = float(area_match.group(1).replace(",", ".")) if area_match else None
    return rooms, area


def format_price(price_rub: int | None) -> str:
    if price_rub is None:
        return "Цена не указана"
    return f"{price_rub:,}".replace(",", " ") + " ₽"


def format_area(area_m2: float | None) -> str:
    if area_m2 is None:
        return "?"
    numeric = float(area_m2)
    if numeric.is_integer():
        whole = int(numeric)
        return str(whole)
    return str(numeric).replace(".", ",")

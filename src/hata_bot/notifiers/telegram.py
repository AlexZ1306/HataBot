from __future__ import annotations

import logging
import json
import mimetypes
from datetime import datetime, timezone
from html import escape
import math

import requests

from hata_bot.exceptions import ConfigError, NotificationError
from hata_bot.fingerprints import format_area, format_price
from hata_bot.http import build_session
from hata_bot.models import CommuteLeg, Listing, SchoolCommute, TelegramConfig
from hata_bot.notifiers.base import Notifier


LOGGER = logging.getLogger("hata_bot.telegram")


class TelegramNotifier(Notifier):
    def __init__(self, config: TelegramConfig, session: requests.Session | None = None) -> None:
        if not config.enabled:
            raise ConfigError("Telegram notifier is disabled in configuration.")
        if not config.bot_token:
            raise ConfigError("HATABOT_TELEGRAM_BOT_TOKEN is missing.")
        if not self._resolve_default_chat_ids(config):
            raise ConfigError("HATABOT_TELEGRAM_CHAT_ID is missing.")

        self.config = config
        self.session = session or build_session(user_agent="HataBot/0.1.0 (+TelegramNotifier)")
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"

    def send_new_listing(self, listing: Listing, *, poll_note: str | None = None) -> None:
        self.send_listing(listing, poll_note=poll_note)

    def send_test_message(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        text = f"<b>HataBot test</b>\n{escape(message)}\n<code>{timestamp}</code>"
        self.send_message(text)

    def healthcheck(self) -> str:
        response = self.session.get(f"{self.base_url}/getMe", timeout=15)
        data = self._parse_response(response)
        username = data.get("result", {}).get("username")
        return str(username or "unknown_bot")

    def send_message(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        for target_chat_id in self._target_chat_ids(chat_id):
            payload = {
                "chat_id": target_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            response = self.session.post(f"{self.base_url}/sendMessage", json=payload, timeout=15)
            self._parse_response(response)

    def send_listing(
        self,
        listing: Listing,
        *,
        poll_note: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        caption = build_new_listing_message(listing, poll_note=poll_note)
        if listing.image_url:
            try:
                self.send_photo_from_url(
                    photo_url=listing.image_url,
                    caption=caption,
                    chat_id=chat_id,
                )
                return
            except NotificationError as exc:
                LOGGER.warning("Falling back to text-only Telegram message for listing %s: %s", listing.external_id, exc)
                pass

        self.send_message(caption, chat_id=chat_id)

    def send_photo(
        self,
        *,
        photo_url: str,
        caption: str,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        for target_chat_id in self._target_chat_ids(chat_id):
            payload = {
                "chat_id": target_chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            response = self.session.post(f"{self.base_url}/sendPhoto", json=payload, timeout=20)
            self._parse_response(response)

    def send_photo_from_url(
        self,
        *,
        photo_url: str,
        caption: str,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        filename, content_type, content = self._download_photo(photo_url)
        self.send_photo_bytes(
            filename=filename,
            content_type=content_type,
            content=content,
            caption=caption,
            chat_id=chat_id,
            reply_markup=reply_markup,
        )

    def send_photo_bytes(
        self,
        *,
        filename: str,
        content_type: str,
        content: bytes,
        caption: str,
        chat_id: str | None = None,
        reply_markup: dict | None = None,
    ) -> None:
        for target_chat_id in self._target_chat_ids(chat_id):
            data: dict[str, object] = {
                "chat_id": target_chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            }
            if reply_markup is not None:
                data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

            files = {
                "photo": (filename, content, content_type),
            }
            response = self.session.post(f"{self.base_url}/sendPhoto", data=data, files=files, timeout=30)
            self._parse_response(response)

    def _download_photo(self, photo_url: str) -> tuple[str, str, bytes]:
        response = self.session.get(photo_url, timeout=20)
        if response.status_code >= 400:
            raise NotificationError(f"Photo download failed with HTTP {response.status_code}")

        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            raise NotificationError(f"Downloaded photo has unexpected content type: {content_type or 'unknown'}")

        extension = mimetypes.guess_extension(content_type) or ".jpg"
        filename = f"listing_photo{extension}"
        return filename, content_type, response.content

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict]:
        params: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 10)
        data = self._parse_response(response)
        result = data.get("result", [])
        return result if isinstance(result, list) else []

    def _parse_response(self, response: requests.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise NotificationError(f"Telegram returned non-JSON response: HTTP {response.status_code}") from exc

        if response.status_code >= 400 or not data.get("ok", False):
            description = data.get("description") or f"HTTP {response.status_code}"
            raise NotificationError(f"Telegram API error: {description}")

        return data

    @staticmethod
    def _resolve_default_chat_ids(config: TelegramConfig) -> list[str]:
        if config.chat_ids:
            return [item for item in config.chat_ids if item]
        if config.chat_id:
            return [config.chat_id]
        return []

    def _target_chat_ids(self, chat_id: str | None) -> list[str]:
        if chat_id:
            return [chat_id]
        return self._resolve_default_chat_ids(self.config)


def build_new_listing_message(listing: Listing, *, poll_note: str | None = None) -> str:
    parts: list[str] = []
    if poll_note:
        parts.append(f"<b>{escape(poll_note)}</b>")
    parts.append(f"<a href=\"{escape(listing.url, quote=True)}\"><b>{escape(listing.title)}</b></a>")

    price_text = format_price(listing.price_rub)
    parts.append(f"Цена: <b>{price_text}</b>")

    size_meta: list[str] = []
    if listing.rooms is not None:
        size_meta.append(f"{listing.rooms} комн.")
    if listing.area_m2 is not None:
        size_meta.append(f"{format_area(listing.area_m2)} м²")
    if size_meta:
        parts.append(f"Параметры: {' • '.join(size_meta)}")

    if listing.address:
        parts.append(f"Адрес: {escape(listing.address)}")

    district_line = _build_district_line(listing)
    if district_line:
        parts.append(district_line)
    if listing.metro:
        parts.append(f"Метро: {escape(listing.metro)}")
    seller_line = _build_seller_line(listing)
    if seller_line:
        parts.append(seller_line)
    commute_lines = _build_school_commute_lines(listing.school_commute)
    if commute_lines:
        parts.extend(commute_lines)
    if listing.published_text:
        parts.append(f"На сайте: {escape(listing.published_text)}")

    return "\n".join(parts)


def build_listings_digest_message(listings: list[Listing], *, title: str) -> str:
    parts = [f"<b>{escape(title)}</b>"]
    for index, listing in enumerate(listings, start=1):
        lines = [f"{index}. <a href=\"{escape(listing.url, quote=True)}\"><b>{escape(listing.title)}</b></a>"]

        lines.append(f"Цена: <b>{format_price(listing.price_rub)}</b>")

        size_meta: list[str] = []
        if listing.rooms is not None:
            size_meta.append(f"{listing.rooms} комн.")
        if listing.area_m2 is not None:
            size_meta.append(f"{format_area(listing.area_m2)} м²")
        if size_meta:
            lines.append(f"Параметры: {' • '.join(size_meta)}")

        if listing.address:
            lines.append(f"Адрес: {escape(listing.address)}")
        district_line = _build_district_line(listing)
        if district_line:
            lines.append(district_line)
        if listing.metro:
            lines.append(f"Метро: {escape(listing.metro)}")
        seller_line = _build_seller_line(listing)
        if seller_line:
            lines.append(seller_line)
        commute_lines = _build_school_commute_lines(listing.school_commute)
        if commute_lines:
            lines.extend(commute_lines)
        if listing.published_text:
            lines.append(f"На сайте: {escape(listing.published_text)}")

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _build_seller_line(listing: Listing) -> str | None:
    seller_name = (listing.seller_name or "").strip()
    raw_payload = listing.raw_payload if isinstance(listing.raw_payload, dict) else {}
    seller_label = str(raw_payload.get("seller_label") or "").strip()

    if listing.seller_kind == "owner":
        if seller_name:
            return f"🔥 Собственник: {escape(seller_name)}"
        return "🔥 Собственник"

    if listing.seller_kind == "agency":
        if seller_name:
            return f"Агентство: {escape(seller_name)}"
        return "Агентство"

    if listing.seller_kind == "company":
        if seller_name:
            return f"Компания: {escape(seller_name)}"
        return "Компания"

    if listing.seller_kind == "agent":
        if seller_name:
            return f"Риелтор: {escape(seller_name)}"
        return "Риелтор"

    if seller_label and seller_name and seller_label.casefold() != seller_name.casefold():
        return f"{escape(seller_label)}: {escape(seller_name)}"
    if seller_label:
        return escape(seller_label)
    if seller_name:
        return f"Продавец: {escape(seller_name)}"
    return None


def _build_school_commute_lines(school_commute: SchoolCommute | None) -> list[str]:
    if school_commute is None:
        return []

    lines = ["До школы:"]
    if school_commute.walking:
        lines.append(f"Пешком: {_format_leg(school_commute.walking)}")
    if school_commute.driving:
        lines.append(f"На машине: {_format_leg(school_commute.driving)}")
    if school_commute.transit:
        lines.append(f"На транспорте: {_format_leg(school_commute.transit)}")
    if school_commute.reference_text and (school_commute.driving or school_commute.transit):
        lines.append(f"Расчёт: {escape(school_commute.reference_text)}")

    return lines if len(lines) > 1 else []


def _build_district_line(listing: Listing) -> str | None:
    raw_payload = listing.raw_payload if isinstance(listing.raw_payload, dict) else {}
    district = raw_payload.get("district")
    if not district:
        return None

    district_text = str(district).strip()
    lowered = district_text.casefold()
    if lowered.startswith("р-н "):
        district_text = district_text[4:].strip()
    elif lowered.startswith("район "):
        district_text = district_text[6:].strip()

    if not district_text:
        return None
    return f"Район: {escape(district_text)}"


def _format_leg(leg: CommuteLeg) -> str:
    parts = [_format_duration(leg.duration_sec)]
    if leg.distance_m is not None:
        parts.append(_format_distance(leg.distance_m))
    return " • ".join(parts)


def _format_duration(duration_sec: int) -> str:
    minutes = max(1, math.ceil(duration_sec / 60))
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    rem_minutes = minutes % 60
    if rem_minutes == 0:
        return f"{hours} ч"
    return f"{hours} ч {rem_minutes} мин"


def _format_distance(distance_m: int) -> str:
    if distance_m < 1000:
        return f"{distance_m} м"
    kilometers = distance_m / 1000
    return f"{kilometers:.1f}".replace(".", ",") + " км"

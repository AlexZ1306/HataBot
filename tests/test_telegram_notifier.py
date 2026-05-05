import pytest

from hata_bot.exceptions import NotificationError
from hata_bot.models import Listing, TelegramConfig
from hata_bot.notifiers.telegram import TelegramNotifier, build_new_listing_message


def make_listing() -> Listing:
    return Listing(
        source_key="avito_nsk_family",
        external_id="123",
        url="https://example.com/123",
        title="3-к. квартира, 104,4 м², 14/21 эт.",
        price_rub=80000,
        rooms=3,
        area_m2=104.4,
        address="ул. Орджоникидзе, 47",
        metro="Площадь Ленина, 11–15 мин.",
        published_text="1 час назад",
        content_fingerprint="fp-1",
        seller_kind="owner",
        seller_name="Александр",
        image_url="https://images.example.com/123.jpg",
        photo_urls=["https://images.example.com/123.jpg"],
        raw_payload={},
    )


def test_build_new_listing_message_contains_key_fields() -> None:
    message = build_new_listing_message(make_listing(), poll_note="Авито test")

    assert "Авито test" in message
    assert "80 000 ₽" in message
    assert "3 комн." in message
    assert "104,4 м²" in message
    assert "ул. Орджоникидзе, 47" in message
    assert "🔥 Собственник: Александр" in message
    assert "В выдаче: 1 час назад" in message


def test_send_new_listing_raises_on_telegram_api_error() -> None:
    class FakeResponse:
        def __init__(self, status_code, payload, headers=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}
            self.content = content

        def json(self):
            return self._payload

    class FakeSession:
        def get(self, url, timeout):
            return FakeResponse(200, {"ok": True}, headers={"content-type": "image/jpeg"}, content=b"jpegdata")

        def post(self, url, timeout, **kwargs):
            return FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})

    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token="token", chat_id="chat"),
        session=FakeSession(),
    )

    with pytest.raises(NotificationError):
        notifier.send_new_listing(make_listing(), poll_note="Авито test")


def test_send_new_listing_falls_back_to_text_when_photo_send_fails() -> None:
    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.headers = {"content-type": "image/jpeg"}
            self.content = b"jpegdata"

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout):
            return FakeResponse(200, {"ok": True})

        def post(self, url, timeout, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/sendPhoto"):
                return FakeResponse(400, {"ok": False, "description": "wrong file identifier/http url specified"})
            return FakeResponse(200, {"ok": True, "result": {}})

    session = FakeSession()
    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token="token", chat_id="chat"),
        session=session,
    )

    notifier.send_new_listing(make_listing(), poll_note="Авито test")

    assert session.calls[0][0].endswith("/sendPhoto")
    assert session.calls[1][0].endswith("/sendMessage")


def test_send_new_listing_uploads_photo_bytes_when_download_succeeds() -> None:
    class FakeResponse:
        def __init__(self, status_code, payload, headers=None, content=b""):
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}
            self.content = content

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.post_calls = []

        def get(self, url, timeout):
            return FakeResponse(200, {"ok": True}, headers={"content-type": "image/jpeg"}, content=b"jpegdata")

        def post(self, url, timeout, **kwargs):
            self.post_calls.append((url, kwargs))
            return FakeResponse(200, {"ok": True, "result": {}})

    session = FakeSession()
    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token="token", chat_id="chat"),
        session=session,
    )

    notifier.send_new_listing(make_listing(), poll_note="Авито test")

    assert session.post_calls[0][0].endswith("/sendPhoto")
    assert "files" in session.post_calls[0][1]
    assert session.post_calls[0][1]["files"]["photo"][1] == b"jpegdata"


def test_send_message_broadcasts_to_multiple_chat_ids() -> None:
    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.headers = {}
            self.content = b""

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.post_calls = []

        def post(self, url, timeout, **kwargs):
            self.post_calls.append((url, kwargs))
            return FakeResponse(200, {"ok": True, "result": {}})

    session = FakeSession()
    notifier = TelegramNotifier(
        TelegramConfig(enabled=True, bot_token="token", chat_id="chat-1", chat_ids=["chat-1", "chat-2"]),
        session=session,
    )

    notifier.send_message("hello")

    assert len(session.post_calls) == 2
    payloads = [call[1]["json"] for call in session.post_calls]
    assert payloads[0]["chat_id"] == "chat-1"
    assert payloads[1]["chat_id"] == "chat-2"

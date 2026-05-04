from __future__ import annotations

from abc import ABC, abstractmethod

from hata_bot.models import Listing


class Notifier(ABC):
    @abstractmethod
    def send_new_listing(self, listing: Listing, *, poll_note: str | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_test_message(self, message: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def healthcheck(self) -> str:
        raise NotImplementedError


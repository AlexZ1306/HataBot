from __future__ import annotations

from abc import ABC, abstractmethod

from hata_bot.models import Listing


class ListingProvider(ABC):
    @abstractmethod
    def fetch(self) -> list[Listing]:
        raise NotImplementedError


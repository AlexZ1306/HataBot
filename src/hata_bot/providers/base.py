from __future__ import annotations

from abc import ABC, abstractmethod

from hata_bot.models import Listing, SourceConfig


class ListingProvider(ABC):
    @abstractmethod
    def fetch(self) -> list[Listing]:
        raise NotImplementedError


def listing_matches_source_filters(listing: Listing, source: SourceConfig) -> bool:
    if source.min_price_rub is not None:
        if listing.price_rub is None or listing.price_rub < source.min_price_rub:
            return False
    if source.max_price_rub is not None:
        if listing.price_rub is None or listing.price_rub > source.max_price_rub:
            return False
    if source.min_area_m2 is not None:
        if listing.area_m2 is None or listing.area_m2 < source.min_area_m2:
            return False
    if source.min_rooms is not None:
        if listing.rooms is None or listing.rooms < source.min_rooms:
            return False
    return True

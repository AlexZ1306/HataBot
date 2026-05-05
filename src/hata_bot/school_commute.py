from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import requests

from hata_bot.fingerprints import normalize_text
from hata_bot.http import build_session
from hata_bot.models import CommuteLeg, Listing, SchoolCommute, SchoolCommuteConfig
from hata_bot.state import StateStore, utc_now_iso


class SchoolCommuteService:
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )
    GEOCODER_URL = "https://catalog.api.2gis.com/3.0/items/geocode"
    ROUTING_URL = "https://routing.api.2gis.com/routing/7.0.0/global"
    PUBLIC_TRANSPORT_URL = "https://routing.api.2gis.com/public_transport/2.0"
    PUBLIC_TRANSPORT_TYPES = [
        "metro",
        "light_metro",
        "suburban_train",
        "tram",
        "bus",
        "trolleybus",
        "shuttle_bus",
        "light_rail",
        "premetro",
    ]

    def __init__(
        self,
        config: SchoolCommuteConfig | None,
        state: StateStore,
        *,
        session: requests.Session | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or SchoolCommuteConfig()
        self.state = state
        self.session = session or build_session(user_agent=self.DEFAULT_USER_AGENT)
        self.logger = logger or logging.getLogger("hata_bot.school_commute")

    def enrich_listing(self, listing: Listing) -> Listing:
        if listing.school_commute is not None:
            return listing
        if not self.config.enabled:
            return listing
        if self.config.destination_lat is None or self.config.destination_lon is None:
            return listing
        if not listing.address:
            return listing

        try:
            point = self._resolve_origin_point(listing)
            if point is None:
                return listing

            school_commute = self._resolve_school_commute(origin_lat=point["lat"], origin_lon=point["lon"])
            if school_commute is None:
                return listing

            return replace(listing, school_commute=school_commute)
        except Exception as exc:
            self.logger.warning(
                "Failed to calculate school commute for %s/%s: %s",
                listing.source_key,
                listing.external_id,
                exc,
            )
            return listing

    def _resolve_origin_point(self, listing: Listing) -> dict[str, float] | None:
        for query in self._build_origin_queries(listing):
            query_key = self._hash_text(query)
            cached = self.state.get_geocode_cache(query_key)
            if cached is not None:
                return {
                    "lat": float(cached["lat"]),
                    "lon": float(cached["lon"]),
                }

            if not self.config.api_key:
                continue

            resolved = self._geocode_query(query)
            if resolved is None:
                continue

            self.state.upsert_geocode_cache(
                query_key=query_key,
                query_text=query,
                lat=resolved["lat"],
                lon=resolved["lon"],
                formatted_address=resolved.get("formatted_address"),
                resolved_at=utc_now_iso(),
            )
            return {
                "lat": resolved["lat"],
                "lon": resolved["lon"],
            }

        return None

    def _resolve_school_commute(self, *, origin_lat: float, origin_lon: float) -> SchoolCommute | None:
        stale_cached = self.state.get_school_commute_cache(self._build_commute_cache_key(origin_lat, origin_lon))
        min_computed_at = (
            datetime.now(timezone.utc) - timedelta(hours=self.config.cache_ttl_hours)
        ).replace(microsecond=0).isoformat()
        fresh_cached = self.state.get_school_commute_cache(
            self._build_commute_cache_key(origin_lat, origin_lon),
            min_computed_at=min_computed_at,
        )
        if fresh_cached is not None:
            return fresh_cached
        if not self.config.api_key:
            return stale_cached

        computed = self._compute_school_commute(origin_lat=origin_lat, origin_lon=origin_lon)
        if computed is None:
            return stale_cached

        self.state.upsert_school_commute_cache(
            cache_key=self._build_commute_cache_key(origin_lat, origin_lon),
            school_commute=computed,
            computed_at=utc_now_iso(),
        )
        return computed

    def _compute_school_commute(self, *, origin_lat: float, origin_lon: float) -> SchoolCommute | None:
        walking = self._fetch_walking_leg(origin_lat=origin_lat, origin_lon=origin_lon)
        driving = self._fetch_driving_leg(origin_lat=origin_lat, origin_lon=origin_lon)
        transit = self._fetch_transit_leg(origin_lat=origin_lat, origin_lon=origin_lon)

        if walking is None and driving is None and transit is None:
            return None

        reference_text = self._build_reference_text()
        return SchoolCommute(
            destination_name=self.config.destination_name,
            reference_text=reference_text,
            walking=walking,
            driving=driving,
            transit=transit,
        )

    def _fetch_walking_leg(self, *, origin_lat: float, origin_lon: float) -> CommuteLeg | None:
        payload = {
            "points": [
                {"type": "walking", "lon": origin_lon, "lat": origin_lat},
                {"type": "walking", "lon": self.config.destination_lon, "lat": self.config.destination_lat},
            ],
            "transport": "walking",
            "output": "summary",
            "locale": "ru",
        }
        data = self._post_json(self.ROUTING_URL, payload)
        return self._extract_routing_leg(data)

    def _fetch_driving_leg(self, *, origin_lat: float, origin_lon: float) -> CommuteLeg | None:
        payload = {
            "points": [
                {"type": "stop", "lon": origin_lon, "lat": origin_lat},
                {"type": "stop", "lon": self.config.destination_lon, "lat": self.config.destination_lat},
            ],
            "transport": "driving",
            "output": "summary",
            "route_mode": "fastest",
            "traffic_mode": "statistics",
            "utc": self._next_weekday_departure_timestamp(),
            "locale": "ru",
        }
        data = self._post_json(self.ROUTING_URL, payload)
        return self._extract_routing_leg(data)

    def _fetch_transit_leg(self, *, origin_lat: float, origin_lon: float) -> CommuteLeg | None:
        payload = {
            "source": {"point": {"lat": origin_lat, "lon": origin_lon}},
            "target": {"point": {"lat": self.config.destination_lat, "lon": self.config.destination_lon}},
            "transport": list(self.PUBLIC_TRANSPORT_TYPES),
            "max_result_count": 3,
            "start_time": self._next_weekday_departure_timestamp(),
            "enable_schedule": False,
            "locale": "ru",
        }
        data = self._post_json(self.PUBLIC_TRANSPORT_URL, payload)
        if not isinstance(data, list):
            return None

        best_duration: int | None = None
        best_leg: CommuteLeg | None = None
        for option in data:
            if not isinstance(option, dict):
                continue
            if option.get("pedestrian") is True:
                continue
            duration = self._as_int(option.get("total_duration"))
            distance = self._as_int(option.get("total_distance"))
            if duration is None:
                continue
            if best_duration is None or duration < best_duration:
                best_duration = duration
                best_leg = CommuteLeg(duration_sec=duration, distance_m=distance)
        return best_leg

    def _geocode_query(self, query: str) -> dict[str, float | str] | None:
        response = self.session.get(
            self.GEOCODER_URL,
            params={
                "q": query,
                "fields": "items.point",
                "key": self.config.api_key,
            },
            timeout=self.config.request_timeout_sec,
        )
        data = self._parse_json_response(response)
        items = ((data.get("result") or {}).get("items")) or []
        if not items:
            return None

        first = items[0]
        if not isinstance(first, dict):
            return None
        point = first.get("point") or {}
        lat = self._as_float(point.get("lat"))
        lon = self._as_float(point.get("lon"))
        if lat is None or lon is None:
            return None

        return {
            "lat": lat,
            "lon": lon,
            "formatted_address": str(first.get("full_name") or first.get("address_name") or "").strip() or None,
        }

    def _post_json(self, url: str, payload: dict) -> dict | list | None:
        response = self.session.post(
            url,
            params={"key": self.config.api_key},
            json=payload,
            timeout=self.config.request_timeout_sec,
        )
        return self._parse_json_response(response)

    @staticmethod
    def _parse_json_response(response: requests.Response) -> dict | list | None:
        if response.status_code >= 400:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _extract_routing_leg(data: dict | list | None) -> CommuteLeg | None:
        if not isinstance(data, dict):
            return None
        routes = data.get("result")
        if not isinstance(routes, list) or not routes:
            return None
        route = routes[0]
        if not isinstance(route, dict):
            return None
        duration = SchoolCommuteService._as_int(route.get("total_duration"))
        if duration is None:
            duration = SchoolCommuteService._as_int(route.get("duration"))

        distance = SchoolCommuteService._as_int(route.get("total_distance"))
        if distance is None:
            distance = SchoolCommuteService._as_int(route.get("length"))
        if duration is None:
            return None
        return CommuteLeg(duration_sec=duration, distance_m=distance)

    def _build_origin_queries(self, listing: Listing) -> list[str]:
        address = normalize_text(listing.address or "")
        district = normalize_text(str((listing.raw_payload or {}).get("district") or ""))
        city = normalize_text(self.config.origin_city_name or "")
        district_label = ""

        if district and district.casefold() not in address.casefold():
            district_label = district if "район" in district.casefold() else f"{district} район"

        candidates = [
            ", ".join(part for part in [address, district_label, city] if part),
            ", ".join(part for part in [address, city] if part),
            address,
        ]

        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = normalize_text(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    def _build_commute_cache_key(self, origin_lat: float, origin_lon: float) -> str:
        raw = (
            f"{origin_lat:.6f},{origin_lon:.6f}|"
            f"{self.config.destination_lat:.6f},{self.config.destination_lon:.6f}|"
            f"{self._build_reference_text()}"
        )
        return self._hash_text(raw)

    def _next_weekday_departure_timestamp(self) -> int:
        hours, minutes = [int(part) for part in self.config.departure_time_local.split(":", 1)]
        now_local = datetime.now().astimezone()
        candidate = now_local.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        while candidate.weekday() >= 5 or candidate <= now_local:
            candidate += timedelta(days=1)
            candidate = candidate.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        return int(candidate.astimezone(timezone.utc).timestamp())

    def _build_reference_text(self) -> str:
        return f"Будний день, {self.config.departure_time_local}"

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _as_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hata_bot.models import Listing


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS seen_listings (
                source_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                price_rub INTEGER,
                rooms INTEGER,
                area_m2 REAL,
                address TEXT,
                metro TEXT,
                published_text TEXT,
                content_fingerprint TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                notification_state TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_notified_at TEXT,
                last_notified_at TEXT,
                PRIMARY KEY (source_key, external_id)
            );

            CREATE INDEX IF NOT EXISTS idx_seen_listings_fingerprint
                ON seen_listings (source_key, content_fingerprint, first_seen_at);

            CREATE INDEX IF NOT EXISTS idx_seen_listings_state
                ON seen_listings (source_key, notification_state, first_seen_at);

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                items_fetched INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                bootstrap INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                duration_ms INTEGER
            );

            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS picker_seen (
                picker_key TEXT NOT NULL,
                listing_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                shown_at TEXT NOT NULL,
                PRIMARY KEY (picker_key, listing_key)
            );

            CREATE TABLE IF NOT EXISTS picker_candidates (
                picker_key TEXT NOT NULL,
                listing_key TEXT NOT NULL,
                source_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY (picker_key, listing_key)
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def count_seen(self, source_key: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM seen_listings WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        return int(row["count"])

    def get_listing_row(self, source_key: str, external_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM seen_listings WHERE source_key = ? AND external_id = ?",
            (source_key, external_id),
        ).fetchone()

    def find_recent_by_fingerprint(self, source_key: str, fingerprint: str, *, seen_since: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT *
            FROM seen_listings
            WHERE source_key = ?
              AND content_fingerprint = ?
              AND first_seen_at >= ?
            ORDER BY first_seen_at DESC
            LIMIT 1
            """,
            (source_key, fingerprint, seen_since),
        ).fetchone()

    def insert_listing(self, listing: Listing, *, notification_state: str, seen_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO seen_listings (
                source_key, external_id, url, title, price_rub, rooms, area_m2, address, metro,
                published_text, content_fingerprint, raw_payload, notification_state,
                first_seen_at, last_seen_at, first_notified_at, last_notified_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                listing.source_key,
                listing.external_id,
                listing.url,
                listing.title,
                listing.price_rub,
                listing.rooms,
                listing.area_m2,
                listing.address,
                listing.metro,
                listing.published_text,
                listing.content_fingerprint,
                json.dumps(self._serialize_raw_payload(listing), ensure_ascii=False),
                notification_state,
                seen_at,
                seen_at,
            ),
        )
        self.connection.commit()

    def update_listing_seen(self, listing: Listing, *, seen_at: str) -> None:
        self.connection.execute(
            """
            UPDATE seen_listings
            SET url = ?,
                title = ?,
                price_rub = ?,
                rooms = ?,
                area_m2 = ?,
                address = ?,
                metro = ?,
                published_text = ?,
                content_fingerprint = ?,
                raw_payload = ?,
                last_seen_at = ?
            WHERE source_key = ? AND external_id = ?
            """,
            (
                listing.url,
                listing.title,
                listing.price_rub,
                listing.rooms,
                listing.area_m2,
                listing.address,
                listing.metro,
                listing.published_text,
                listing.content_fingerprint,
                json.dumps(self._serialize_raw_payload(listing), ensure_ascii=False),
                seen_at,
                listing.source_key,
                listing.external_id,
            ),
        )
        self.connection.commit()

    def get_pending_notifications(self, source_key: str) -> list[Listing]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM seen_listings
            WHERE source_key = ?
              AND notification_state = 'pending'
            ORDER BY first_seen_at ASC, external_id ASC
            """,
            (source_key,),
        ).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def mark_notified(self, source_key: str, external_id: str, *, notified_at: str) -> None:
        self.connection.execute(
            """
            UPDATE seen_listings
            SET notification_state = 'sent',
                first_notified_at = COALESCE(first_notified_at, ?),
                last_notified_at = ?
            WHERE source_key = ? AND external_id = ?
            """,
            (notified_at, notified_at, source_key, external_id),
        )
        self.connection.commit()

    def suppress_pending_notifications(self, source_key: str) -> None:
        self.connection.execute(
            """
            UPDATE seen_listings
            SET notification_state = 'suppressed'
            WHERE source_key = ?
              AND notification_state = 'pending'
            """,
            (source_key,),
        )
        self.connection.commit()

    def suppress_listing_notification(self, source_key: str, external_id: str) -> None:
        self.connection.execute(
            """
            UPDATE seen_listings
            SET notification_state = 'suppressed'
            WHERE source_key = ?
              AND external_id = ?
              AND notification_state = 'pending'
            """,
            (source_key, external_id),
        )
        self.connection.commit()

    def start_run(self, source_key: str, *, started_at: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO runs (source_key, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (source_key, started_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        items_fetched: int,
        new_count: int,
        bootstrap: bool,
        error_message: str | None,
        duration_ms: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET finished_at = ?,
                status = ?,
                items_fetched = ?,
                new_count = ?,
                bootstrap = ?,
                error_message = ?,
                duration_ms = ?
            WHERE id = ?
            """,
            (finished_at, status, items_fetched, new_count, 1 if bootstrap else 0, error_message, duration_ms, run_id),
        )
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        now = utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self.connection.commit()

    def get_picker_seen_keys(self, picker_key: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT listing_key FROM picker_seen WHERE picker_key = ?",
            (picker_key,),
        ).fetchall()
        return {str(row["listing_key"]) for row in rows}

    def mark_picker_seen(self, *, picker_key: str, listing_key: str, source_key: str, external_id: str, shown_at: str) -> None:
        self.connection.execute(
            """
            INSERT INTO picker_seen (picker_key, listing_key, source_key, external_id, shown_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(picker_key, listing_key) DO UPDATE SET
                source_key = excluded.source_key,
                external_id = excluded.external_id,
                shown_at = excluded.shown_at
            """,
            (picker_key, listing_key, source_key, external_id, shown_at),
        )
        self.connection.commit()

    def clear_picker_seen(self, picker_key: str) -> None:
        self.connection.execute(
            "DELETE FROM picker_seen WHERE picker_key = ?",
            (picker_key,),
        )
        self.connection.commit()

    def replace_picker_candidates(self, *, picker_key: str, listings: list[Listing], refreshed_at: str) -> None:
        self.connection.execute("DELETE FROM picker_candidates WHERE picker_key = ?", (picker_key,))
        rows = [
            (
                picker_key,
                listing.content_fingerprint,
                listing.source_key,
                listing.external_id,
                json.dumps(self._serialize_listing(listing), ensure_ascii=False),
                refreshed_at,
            )
            for listing in listings
        ]
        self.connection.executemany(
            """
            INSERT INTO picker_candidates (picker_key, listing_key, source_key, external_id, payload_json, refreshed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.connection.commit()

    def get_picker_candidates(self, picker_key: str) -> list[Listing]:
        rows = self.connection.execute(
            """
            SELECT payload_json
            FROM picker_candidates
            WHERE picker_key = ?
            ORDER BY refreshed_at DESC, source_key ASC, external_id ASC
            """,
            (picker_key,),
        ).fetchall()
        return [self._listing_from_payload_json(row["payload_json"]) for row in rows]

    def get_picker_candidates_refreshed_at(self, picker_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT MAX(refreshed_at) AS refreshed_at FROM picker_candidates WHERE picker_key = ?",
            (picker_key,),
        ).fetchone()
        if row is None or row["refreshed_at"] is None:
            return None
        return str(row["refreshed_at"])

    def get_seen_listings(self, source_keys: list[str]) -> list[Listing]:
        if not source_keys:
            return []
        placeholders = ", ".join("?" for _ in source_keys)
        rows = self.connection.execute(
            f"""
            SELECT *
            FROM seen_listings
            WHERE source_key IN ({placeholders})
            ORDER BY last_seen_at DESC, first_seen_at DESC
            """,
            tuple(source_keys),
        ).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def _row_to_listing(self, row: sqlite3.Row) -> Listing:
        raw_payload = json.loads(row["raw_payload"]) if row["raw_payload"] else {}
        return Listing(
            source_key=row["source_key"],
            external_id=row["external_id"],
            url=row["url"],
            title=row["title"],
            price_rub=row["price_rub"],
            rooms=row["rooms"],
            area_m2=row["area_m2"],
            address=row["address"],
            metro=row["metro"],
            published_text=row["published_text"],
            content_fingerprint=row["content_fingerprint"],
            seller_kind=raw_payload.get("seller_kind"),
            seller_name=raw_payload.get("seller_name"),
            image_url=raw_payload.get("image_url"),
            photo_urls=raw_payload.get("photo_urls", []),
            raw_payload=raw_payload,
        )

    @staticmethod
    def _serialize_raw_payload(listing: Listing) -> dict:
        payload = dict(listing.raw_payload)
        payload["seller_kind"] = listing.seller_kind
        payload["seller_name"] = listing.seller_name
        payload["image_url"] = listing.image_url
        payload["photo_urls"] = list(listing.photo_urls)
        return payload

    @classmethod
    def _serialize_listing(cls, listing: Listing) -> dict:
        return {
            "source_key": listing.source_key,
            "external_id": listing.external_id,
            "url": listing.url,
            "title": listing.title,
            "price_rub": listing.price_rub,
            "rooms": listing.rooms,
            "area_m2": listing.area_m2,
            "address": listing.address,
            "metro": listing.metro,
            "published_text": listing.published_text,
            "content_fingerprint": listing.content_fingerprint,
            "seller_kind": listing.seller_kind,
            "seller_name": listing.seller_name,
            "image_url": listing.image_url,
            "photo_urls": list(listing.photo_urls),
            "raw_payload": cls._serialize_raw_payload(listing),
        }

    @classmethod
    def _listing_from_payload_json(cls, payload_json: str) -> Listing:
        payload = json.loads(payload_json)
        return Listing(
            source_key=payload["source_key"],
            external_id=payload["external_id"],
            url=payload["url"],
            title=payload["title"],
            price_rub=payload.get("price_rub"),
            rooms=payload.get("rooms"),
            area_m2=payload.get("area_m2"),
            address=payload.get("address"),
            metro=payload.get("metro"),
            published_text=payload.get("published_text"),
            content_fingerprint=payload["content_fingerprint"],
            seller_kind=payload.get("seller_kind"),
            seller_name=payload.get("seller_name"),
            image_url=payload.get("image_url"),
            photo_urls=payload.get("photo_urls", []),
            raw_payload=payload.get("raw_payload", {}),
        )

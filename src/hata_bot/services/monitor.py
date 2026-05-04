from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from hata_bot.exceptions import ConfigError
from hata_bot.models import Listing, RunResult, Settings, SourceConfig
from hata_bot.notifiers.base import Notifier
from hata_bot.providers.avito import AvitoProvider
from hata_bot.state import StateStore


class MonitorService:
    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        notifier: Notifier,
        *,
        logger: logging.Logger | None = None,
        provider_factory=None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.notifier = notifier
        self.logger = logger or logging.getLogger("hata_bot.monitor")
        self.provider_factory = provider_factory or self._build_provider

    def run(self, *, source_key: str | None = None) -> list[RunResult]:
        sources = [self._get_source(source_key)] if source_key else [source for source in self.settings.sources if source.enabled]
        results: list[RunResult] = []
        for source in sources:
            results.append(self.run_source(source))
        return results

    def run_source(self, source: SourceConfig) -> RunResult:
        started = datetime.now(timezone.utc)
        started_iso = started.replace(microsecond=0).isoformat()
        run_id = self.state.start_run(source.source_key, started_at=started_iso)
        bootstrap = False
        items_fetched = 0
        new_count = 0

        try:
            provider = self.provider_factory(source)
            listings = provider.fetch()
            items_fetched = len(listings)

            bootstrap = self.state.count_seen(source.source_key) == 0
            if bootstrap:
                for listing in listings:
                    self.state.insert_listing(listing, notification_state="baseline", seen_at=started_iso)
                self.logger.info("Seeded baseline for %s with %s listings", source.source_key, len(listings))
                result = RunResult(source.source_key, "bootstrap", items_fetched, 0, True, None)
            else:
                self._ingest_listings(source, listings, seen_at=started_iso)
                pending = self.state.get_pending_notifications(source.source_key)
                for listing in pending:
                    self.notifier.send_new_listing(listing, poll_note=source.poll_note)
                    self.state.mark_notified(source.source_key, listing.external_id, notified_at=started_iso)
                    new_count += 1
                result = RunResult(source.source_key, "ok", items_fetched, new_count, False, None)

            finished = datetime.now(timezone.utc)
            self.state.finish_run(
                run_id,
                finished_at=finished.replace(microsecond=0).isoformat(),
                status=result.status,
                items_fetched=items_fetched,
                new_count=new_count,
                bootstrap=bootstrap,
                error_message=None,
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
            return result
        except Exception as exc:
            finished = datetime.now(timezone.utc)
            self.state.finish_run(
                run_id,
                finished_at=finished.replace(microsecond=0).isoformat(),
                status="error",
                items_fetched=items_fetched,
                new_count=new_count,
                bootstrap=bootstrap,
                error_message=str(exc),
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
            self.logger.exception("Run failed for source %s", source.source_key)
            raise

    def _ingest_listings(self, source: SourceConfig, listings: list[Listing], *, seen_at: str) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=source.repost_suppression_days)
        cutoff_iso = cutoff.replace(microsecond=0).isoformat()

        for listing in listings:
            existing = self.state.get_listing_row(source.source_key, listing.external_id)
            if existing:
                self.state.update_listing_seen(listing, seen_at=seen_at)
                continue

            recent_duplicate = self.state.find_recent_by_fingerprint(
                source.source_key,
                listing.content_fingerprint,
                seen_since=cutoff_iso,
            )
            state = "suppressed" if recent_duplicate else "pending"
            self.state.insert_listing(listing, notification_state=state, seen_at=seen_at)

    def _get_source(self, source_key: str) -> SourceConfig:
        for source in self.settings.sources:
            if source.source_key == source_key:
                return source
        raise ConfigError(f"Unknown source_key: {source_key}")

    def _build_provider(self, source: SourceConfig):
        if source.provider == "avito":
            return AvitoProvider(source)
        raise ConfigError(f"Unsupported provider: {source.provider}")


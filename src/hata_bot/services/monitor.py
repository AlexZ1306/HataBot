from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from hata_bot.exceptions import ConfigError
from hata_bot.models import Listing, RunResult, Settings, SourceConfig
from hata_bot.notifiers.base import Notifier
from hata_bot.providers.avito import AvitoProvider
from hata_bot.providers.cian import CianProvider
from hata_bot.providers.domclick import DomclickProvider
from hata_bot.search_profile import apply_search_profile_to_source, build_source_profile_signature, load_search_profile
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
        profile = load_search_profile(self.settings, self.state)
        sources = [self._get_source(source_key, profile)] if source_key else self._get_enabled_sources(profile)
        results: list[RunResult] = []
        for source in sources:
            results.append(self.run_source(source))
        return results

    def run_source(self, source: SourceConfig) -> RunResult:
        started = datetime.now(timezone.utc)
        started_iso = started.replace(microsecond=0).isoformat()
        run_id = self.state.start_run(source.source_key, started_at=started_iso)
        bootstrap = False
        bootstrap_reason: str | None = None
        items_fetched = 0
        new_count = 0
        scanned_count = 0
        matched_count = 0
        pages_checked = 0

        try:
            provider = self.provider_factory(source)
            listings = provider.fetch()
            items_fetched = len(listings)
            fetch_stats = getattr(provider, "last_fetch_stats", None)
            if fetch_stats is not None:
                scanned_count = fetch_stats.scanned_count
                matched_count = fetch_stats.matched_count
                pages_checked = fetch_stats.pages_checked
            else:
                scanned_count = items_fetched
                matched_count = items_fetched
                pages_checked = source.max_pages

            source_signature = build_source_profile_signature(source)
            source_signature_key = self._source_signature_key(source.source_key)
            previous_signature = self.state.get_meta(source_signature_key)
            existing_count = self.state.count_seen(source.source_key)
            bootstrap = existing_count == 0 or previous_signature != source_signature

            if bootstrap:
                bootstrap_reason = "initial" if existing_count == 0 else "profile_changed"
                self._seed_baseline(source, listings, seen_at=started_iso)
                self.state.set_meta(source_signature_key, source_signature)
                self.logger.info(
                    "Seeded baseline for %s with %s listings (reason=%s)",
                    source.source_key,
                    len(listings),
                    bootstrap_reason,
                )
                result = RunResult(
                    source_key=source.source_key,
                    status="bootstrap",
                    items_fetched=items_fetched,
                    new_count=0,
                    bootstrap=True,
                    bootstrap_reason=bootstrap_reason,
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    pages_checked=pages_checked,
                )
            else:
                self._ingest_listings(source, listings, seen_at=started_iso)
                pending = self.state.get_pending_notifications(source.source_key)
                for listing in pending:
                    self.notifier.send_new_listing(listing, poll_note=source.poll_note)
                    self.state.mark_notified(source.source_key, listing.external_id, notified_at=started_iso)
                    new_count += 1
                self.state.set_meta(source_signature_key, source_signature)
                result = RunResult(
                    source_key=source.source_key,
                    status="ok",
                    items_fetched=items_fetched,
                    new_count=new_count,
                    bootstrap=False,
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    pages_checked=pages_checked,
                )

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

    def _get_source(self, source_key: str, profile) -> SourceConfig:
        for source in self.settings.sources:
            if source.source_key == source_key:
                return apply_search_profile_to_source(source, profile)
        raise ConfigError(f"Unknown source_key: {source_key}")

    def _get_enabled_sources(self, profile) -> list[SourceConfig]:
        sources: list[SourceConfig] = []
        for source in self.settings.sources:
            effective_source = apply_search_profile_to_source(source, profile)
            if effective_source.enabled:
                sources.append(effective_source)
        return sources

    def _seed_baseline(self, source: SourceConfig, listings: list[Listing], *, seen_at: str) -> None:
        self.state.suppress_pending_notifications(source.source_key)
        for listing in listings:
            existing = self.state.get_listing_row(source.source_key, listing.external_id)
            if existing:
                self.state.update_listing_seen(listing, seen_at=seen_at)
                continue
            self.state.insert_listing(listing, notification_state="baseline", seen_at=seen_at)

    @staticmethod
    def _source_signature_key(source_key: str) -> str:
        return f"search_profile_signature:{source_key}"

    def _build_provider(self, source: SourceConfig):
        if source.provider == "avito":
            return AvitoProvider(source)
        if source.provider == "cian":
            return CianProvider(source, data_dir=self.settings.app.data_dir)
        if source.provider == "domclick":
            return DomclickProvider(source, data_dir=self.settings.app.data_dir)
        raise ConfigError(f"Unsupported provider: {source.provider}")

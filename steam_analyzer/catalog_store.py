"""Reading and writing the release catalog in PostgreSQL."""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from steam_analyzer import steam_sources as steam
from steam_analyzer.catalog import AnnouncementEvent, Release, classify, load_overrides
from steam_analyzer.http import make_session

log = logging.getLogger(__name__)

OVERRIDES_FILE = Path(__file__).resolve().parent.parent / "config" / "release_overrides.json"
DUPLICATE_MERGE_HOURS = 12
MAX_EVENT_PAGES = 50

FetchPage = Callable[[str | None], list[dict]]  # older_than_gid -> events, newest first


@dataclass(frozen=True)
class RefreshResult:
    refresh_id: int
    status: str
    events_seen: int
    pages_fetched: int
    news_feed_count: int | None
    error: str | None = None


def refresh_catalog(
    conn: psycopg.Connection,
    app_id: int,
    fetch_page: FetchPage,
    fetch_news_count: Callable[[], int] | None = None,
) -> RefreshResult:
    """Read the full announcement history, newest first, until a page brings nothing new.

    Each page is committed as it arrives, so a failure keeps what was read and is recorded as 'failed'.
    Only a 'complete' refresh counts as verifying the catalog.
    """
    started = datetime.now(timezone.utc)
    refresh_id = conn.execute(
        "INSERT INTO catalog_refreshes (app_id, started_at, status) VALUES (%s, %s, 'running') RETURNING refresh_id",
        (app_id, started),
    ).fetchone()[0]
    conn.commit()

    seen: set[str] = set()
    pages = 0
    anchor = None
    try:
        while True:
            if pages >= MAX_EVENT_PAGES:
                raise RuntimeError(f"stopped after {MAX_EVENT_PAGES} pages without reaching the oldest event")
            events = fetch_page(anchor)
            pages += 1
            new = [e for e in events if str(e["gid"]) not in seen]
            if not new:
                break
            for raw in new:
                _upsert_event(conn, app_id, refresh_id, raw, observed_at=started)
                seen.add(str(raw["gid"]))
            conn.commit()
            anchor = str(min(new, key=lambda e: e["rtime32_start_time"])["gid"])
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE catalog_refreshes SET status = 'failed', completed_at = now(), pages_fetched = %s,"
            " events_seen = %s, error = %s WHERE refresh_id = %s",
            (pages, len(seen), str(exc), refresh_id),
        )
        conn.commit()
        log.warning("catalog refresh %d failed after %d pages: %s", refresh_id, pages, exc)
        return RefreshResult(refresh_id, "failed", len(seen), pages, None, str(exc))

    news_count = None
    if fetch_news_count:
        try:
            news_count = fetch_news_count()
        except Exception as exc:  # the cross-check is informational; it must not fail the catalog
            log.warning("news count cross-check unavailable: %s", exc)

    conn.execute(
        """UPDATE catalog_refreshes SET status = 'complete', completed_at = now(), pages_fetched = %(pages)s,
               events_seen = %(seen)s, news_feed_count = %(news)s,
               oldest_event_at = (SELECT min(published_at) FROM source_events WHERE last_seen_refresh = %(id)s),
               newest_event_at = (SELECT max(published_at) FROM source_events WHERE last_seen_refresh = %(id)s)
           WHERE refresh_id = %(id)s""",
        {"pages": pages, "seen": len(seen), "news": news_count, "id": refresh_id},
    )
    conn.commit()
    return RefreshResult(refresh_id, "complete", len(seen), pages, news_count)


def live_refresh(conn: psycopg.Connection, app_id: int) -> RefreshResult:
    session = make_session()
    return refresh_catalog(
        conn,
        app_id,
        fetch_page=lambda anchor: steam.fetch_events_page(session, app_id, older_than_gid=anchor),
        fetch_news_count=lambda: steam.fetch_announcement_count(session, app_id),
    )


def _upsert_event(conn: psycopg.Connection, app_id: int, refresh_id: int, raw: dict, observed_at: datetime) -> None:
    event = AnnouncementEvent.from_steam(raw)
    body_fields = dict(raw.get("announcement_body") or {})
    body = body_fields.pop("body", None)
    conn.execute(
        """INSERT INTO source_events (event_gid, app_id, event_type, title, announcement_gid, forum_topic_id,
                                      published_at, event_start_at, body_updated_at, hidden, raw,
                                      first_seen_refresh, last_seen_refresh)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (event_gid) DO UPDATE SET
               event_type = EXCLUDED.event_type, title = EXCLUDED.title,
               announcement_gid = EXCLUDED.announcement_gid, forum_topic_id = EXCLUDED.forum_topic_id,
               published_at = EXCLUDED.published_at, event_start_at = EXCLUDED.event_start_at,
               body_updated_at = EXCLUDED.body_updated_at, hidden = EXCLUDED.hidden,
               raw = EXCLUDED.raw, last_seen_refresh = EXCLUDED.last_seen_refresh""",
        (event.event_gid, app_id, event.event_type, event.title, event.announcement_gid, event.forum_topic_id,
         event.published_at, event.event_start_at, event.body_updated_at, event.hidden,
         Jsonb({**raw, "announcement_body": body_fields}), refresh_id, refresh_id),
    )
    if body is not None:
        conn.execute(
            """INSERT INTO update_note_versions (event_gid, body, body_sha256, source_updated_at, first_observed_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_gid, body_sha256) DO NOTHING""",
            (event.event_gid, body, hashlib.sha256(body.encode("utf-8")).hexdigest(),
             event.body_updated_at, observed_at),
        )


@dataclass(frozen=True)
class CatalogEntry:
    update_id: int
    release: Release


@dataclass(frozen=True)
class Catalog:
    entries: list[CatalogEntry]
    non_releases: list[AnnouncementEvent]
    refresh_id: int | None
    verified_through: datetime | None  # start of the last complete refresh
    events_seen: int | None
    news_feed_count: int | None

    @property
    def coverage_warning(self) -> str | None:
        if self.refresh_id is None:
            return "The catalog has never been loaded."
        if self.news_feed_count is not None and self.news_feed_count != self.events_seen:
            return (f"The events feed returned {self.events_seen} announcements but the news API reports "
                    f"{self.news_feed_count}; some releases may be missing.")
        return None


def load_catalog(conn: psycopg.Connection, app_id: int, overrides_path: Path = OVERRIDES_FILE) -> Catalog:
    """Classify the events from the last complete refresh (plus any seen since) and sync the updates table."""
    last = conn.execute(
        "SELECT refresh_id, started_at, events_seen, news_feed_count FROM catalog_refreshes"
        " WHERE app_id = %s AND status = 'complete' ORDER BY refresh_id DESC LIMIT 1",
        (app_id,),
    ).fetchone()
    if last is None:
        return Catalog([], [], None, None, None, None)
    refresh_id, started_at, events_seen, news_count = last
    raws = conn.execute(
        "SELECT raw FROM source_events WHERE app_id = %s AND last_seen_refresh >= %s", (app_id, refresh_id)
    ).fetchall()
    releases, non_releases = classify(
        [AnnouncementEvent.from_steam(r[0]) for r in raws], load_overrides(overrides_path), DUPLICATE_MERGE_HOURS
    )

    entries = []
    for release in releases:
        update_id = conn.execute(
            """INSERT INTO updates (app_id, primary_event_gid, title, version, release_type, released_at,
                                    released_at_source, is_current)
               VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
               ON CONFLICT (primary_event_gid) DO UPDATE SET
                   title = EXCLUDED.title, version = EXCLUDED.version, release_type = EXCLUDED.release_type,
                   released_at = EXCLUDED.released_at, released_at_source = EXCLUDED.released_at_source,
                   is_current = TRUE
               RETURNING update_id""",
            (app_id, release.key, release.title, release.version, release.release_type,
             release.released_at, release.released_at_source),
        ).fetchone()[0]
        conn.execute("DELETE FROM update_sources WHERE update_id = %s", (update_id,))
        conn.cursor().executemany(
            "INSERT INTO update_sources (update_id, event_gid, role) VALUES (%s, %s, %s)",
            [(update_id, release.key, "primary")] + [(update_id, d.event_gid, "duplicate_post") for d in release.duplicates],
        )
        entries.append(CatalogEntry(update_id, release))
    conn.execute(
        "UPDATE updates SET is_current = FALSE WHERE app_id = %s AND NOT (primary_event_gid = ANY(%s))",
        (app_id, [r.key for r in releases]),
    )
    conn.commit()
    return Catalog(entries, non_releases, refresh_id, started_at, events_seen, news_count)

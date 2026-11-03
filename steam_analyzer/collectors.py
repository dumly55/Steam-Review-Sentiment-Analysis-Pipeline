"""Review and announcement-comment collectors for one run update.

Each collector works on one run_sources row. Every page is stored in the same transaction as its
checkpoint, so an interrupted collection resumes at the next unread page without duplicating anything.
A source ends as:
    complete     paging ended and the items collected reach Steam's own reported total
    partial      some pages were read, but paging failed, hit a cap or limit, or fell short of the total
    unavailable  nothing could be read
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from steam_analyzer.feedback import FeedbackRecord, comment_record, review_record, sha256_text
from steam_analyzer.steam_sources import parse_comment_page

log = logging.getLogger(__name__)

FetchReviews = Callable[[str, tuple[int, int]], dict]  # (cursor, (start, end) unix seconds inclusive) -> response
FetchCommentPage = Callable[[str, int], str]           # (thread id, page number) -> page HTML


@dataclass(frozen=True)
class Sources:
    """Where pages come from: live Steam, or synthetic pages in tests and the demo."""

    fetch_reviews: FetchReviews
    fetch_comment_page: FetchCommentPage


@dataclass
class SourceProgress:
    run_source_id: int
    run_id: int
    update_id: int
    status: str
    next_cursor: str | None
    pages: int
    items: int
    reported_total: int | None


def load_progress(conn: psycopg.Connection, run_source_id: int) -> SourceProgress:
    row = conn.execute(
        """SELECT run_source_id, run_id, update_id, status, next_cursor, pages_fetched, items_collected, reported_total
           FROM run_sources WHERE run_source_id = %s""", (run_source_id,)).fetchone()
    return SourceProgress(*row)


def _save(conn: psycopg.Connection, p: SourceProgress) -> None:
    conn.execute(
        """UPDATE run_sources SET status = %s, next_cursor = %s, pages_fetched = %s, items_collected = %s,
                  reported_total = %s, started_at = COALESCE(started_at, now())
           WHERE run_source_id = %s""",
        (p.status, p.next_cursor, p.pages, p.items, p.reported_total, p.run_source_id))
    conn.commit()


def _finish(conn: psycopg.Connection, p: SourceProgress, status: str, detail: str, capped: bool = False) -> str:
    p.status = status
    conn.execute("UPDATE run_sources SET detail = %s, capped = %s, completed_at = now() WHERE run_source_id = %s",
                 (detail, capped, p.run_source_id))
    _save(conn, p)
    log.info("source %d %s: %s", p.run_source_id, status, detail)
    return status


def _finish_by_count(conn: psycopg.Connection, p: SourceProgress) -> str:
    if p.reported_total is None:
        return _finish(conn, p, "partial", f"collected {p.items}; Steam reported no total to verify against")
    if p.items >= p.reported_total:
        return _finish(conn, p, "complete", f"collected {p.items} of {p.reported_total} reported")
    return _finish(conn, p, "partial", f"collected {p.items} of {p.reported_total} reported")


def store_feedback(conn: psycopg.Connection, p: SourceProgress, app_id: int, record: FeedbackRecord,
                   observed_at: datetime) -> bool:
    """Store the item and this version of it, and add it to the run. Returns False if the run already had it."""
    feedback_id = conn.execute(
        """INSERT INTO feedback_items (app_id, source_type, source_item_id, author_id, created_at, thread_id,
                                       event_gid, permalink, first_observed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (app_id, source_type, source_item_id)
           DO UPDATE SET author_id = COALESCE(feedback_items.author_id, EXCLUDED.author_id)
           RETURNING feedback_id""",
        (app_id, record.source_type, record.source_item_id, record.author_id, record.created_at, record.thread_id,
         record.event_gid, record.permalink, observed_at),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO feedback_versions (feedback_id, text, text_sha256, voted_up, language, edited_at,
                                          first_observed_at, last_observed_at, raw)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT ON CONSTRAINT feedback_versions_identity DO UPDATE SET last_observed_at = EXCLUDED.last_observed_at""",
        (feedback_id, record.text, sha256_text(record.text), record.voted_up, record.language, record.edited_at,
         observed_at, observed_at, Jsonb(record.raw)),
    )
    return conn.execute(
        """INSERT INTO run_feedback (run_id, update_id, source_type, source_item_id, feedback_id)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1""",
        (p.run_id, p.update_id, record.source_type, record.source_item_id, feedback_id),
    ).fetchone() is not None


def collect_reviews(
    conn: psycopg.Connection, run_source_id: int, app_id: int, window_start: datetime, window_end: datetime,
    fetch: FetchReviews, max_items: int | None, max_pages: int, page_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Page through every review created in the window, newest first, by cursor."""
    p = load_progress(conn, run_source_id)
    if p.status == "complete":
        return p.status
    date_range = (int(window_start.timestamp()), math.ceil(window_end.timestamp()) - 1)  # Steam's end is inclusive
    cursor = p.next_cursor or "*"
    p.status = "collecting"
    _save(conn, p)
    while True:
        if max_items is not None and p.items >= max_items:
            return _finish(conn, p, "partial", f"stopped at the cap of {max_items} items", capped=True)
        if p.pages >= max_pages:
            return _finish(conn, p, "partial", f"stopped at the limit of {max_pages} pages")
        try:
            page = fetch(cursor, date_range)
        except Exception as exc:
            return _finish(conn, p, "partial" if p.pages else "unavailable", f"request failed: {exc}")
        p.pages += 1
        if p.reported_total is None and page.get("total_matching") is not None:
            p.reported_total = int(page["total_matching"])
        reviews = page.get("reviews") or []
        if max_items is not None:
            reviews = reviews[:max_items - p.items]
        observed = datetime.now(timezone.utc)
        new = sum(store_feedback(conn, p, app_id, review_record(r, app_id), observed) for r in reviews)
        p.items += new
        next_cursor = page.get("cursor")
        # Stop on an empty page, and also if Steam repeats a page or a cursor, so paging can never loop.
        if not reviews or new == 0 or not next_cursor or next_cursor == cursor:
            return _finish_by_count(conn, p)
        p.next_cursor = cursor = next_cursor
        _save(conn, p)
        sleep(page_delay)


def collect_comments(
    conn: psycopg.Connection, run_source_id: int, app_id: int, thread_id: str, event_gid: str,
    fetch: FetchCommentPage, max_items: int | None, max_pages: int, page_delay: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Read the whole public comment thread, oldest first, 15 comments per page.

    Comments posted after the window are collected too and then excluded by the window check, so the
    thread's total can be verified and the exclusions are visible.
    """
    p = load_progress(conn, run_source_id)
    if p.status == "complete":
        return p.status
    page_no = int(p.next_cursor or 1)
    p.status = "collecting"
    _save(conn, p)
    while True:
        if max_items is not None and p.items >= max_items:
            return _finish(conn, p, "partial", f"stopped at the cap of {max_items} items", capped=True)
        if p.pages >= max_pages:
            return _finish(conn, p, "partial", f"stopped at the limit of {max_pages} pages")
        try:
            comments, total = parse_comment_page(fetch(thread_id, page_no))
        except Exception as exc:
            return _finish(conn, p, "partial" if p.pages else "unavailable", f"page {page_no} failed: {exc}")
        p.pages += 1
        if total is not None:
            p.reported_total = total
        if max_items is not None:
            comments = comments[:max_items - p.items]
        observed = datetime.now(timezone.utc)
        new = sum(store_feedback(conn, p, app_id, comment_record(c, app_id, thread_id, event_gid, page_no), observed)
                  for c in comments)
        p.items += new
        if not comments or new == 0:
            return _finish_by_count(conn, p)
        page_no += 1
        p.next_cursor = str(page_no)
        _save(conn, p)
        sleep(page_delay)

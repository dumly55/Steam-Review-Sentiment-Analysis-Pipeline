"""Collector paging, checkpoints and source states, against the local PostgreSQL with synthetic pages.

Uses the fake app id 999000001, deletes everything it creates, and is skipped without PostgreSQL.
"""

import time

import psycopg
import pytest
import requests
from db_helpers import DATABASE_URL, cleanup_app, event, pager, postgres_reachable, quick_analysis

from steam_analyzer.catalog_store import load_catalog, refresh_catalog
from steam_analyzer.collectors import collect_comments, collect_reviews
from steam_analyzer.db import init_db
from steam_analyzer.demo import DemoComment, render_comment_page
from steam_analyzer.runs import create_run

TEST_APP = 999_000_001
DAY = 86_400
BASE = int(time.time()) - 30 * DAY
TIMELINE = [event("c1", BASE, "Update 1.0.0", 14), event("c2", BASE + 10 * DAY, "Update 1.1.0", 13)]
NO_LIMITS = dict(max_items=None, max_pages=100, page_delay=0)

pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="local PostgreSQL not reachable")


@pytest.fixture
def ctx(tmp_path):
    """A frozen run for Update 1.0.0, whose window is [BASE, BASE + 10 days)."""
    init_db(DATABASE_URL)
    with psycopg.connect(DATABASE_URL) as conn:
        cleanup_app(conn, TEST_APP)
        conn.execute("INSERT INTO games (app_id, name) VALUES (%s, 'Test game')", (TEST_APP,))
        conn.commit()
        refresh_catalog(conn, TEST_APP, pager(TIMELINE))
        update_id = {e.release.title: e.update_id for e in load_catalog(conn, TEST_APP, tmp_path / "x").entries}["Update 1.0.0"]
        run_id = create_run(conn, TEST_APP, [update_id], None, tmp_path / "x", quick_analysis())
        start, end = conn.execute("SELECT window_start, window_end FROM run_updates WHERE run_id = %s",
                                  (run_id,)).fetchone()
        yield conn, run_id, update_id, start, end
        cleanup_app(conn, TEST_APP)


def new_source(conn, run_id, update_id, source_type, event_gid=None):
    row = conn.execute("""INSERT INTO run_sources (run_id, update_id, source_type, event_gid, thread_id, status)
                          VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING run_source_id""",
                       (run_id, update_id, source_type, event_gid, event_gid and "thread")).fetchone()[0]
    conn.commit()
    return row


def source_state(conn, source_id):
    return conn.execute("SELECT status, items_collected, reported_total, next_cursor, capped, detail FROM run_sources"
                        " WHERE run_source_id = %s", (source_id,)).fetchone()


def members(conn, run_id):
    return conn.execute("SELECT count(*) FROM run_feedback WHERE run_id = %s", (run_id,)).fetchone()[0]


def reviews(n, text="Good update"):
    return [{"recommendationid": str(500 + i), "author": {"steamid": str(76561198000000000 + i)}, "language": "english",
             "review": text, "timestamp_created": BASE + (i + 1) * 3600, "timestamp_updated": BASE + (i + 1) * 3600,
             "voted_up": True} for i in range(n)]


def review_api(items, page_size=2, fail_at=None, total=None):
    """Cursor paging like Steam: the cursor is an offset here; an empty page ends the listing."""
    calls = []

    def fetch(cursor, date_range):
        calls.append(cursor)
        offset = 0 if cursor == "*" else int(cursor)
        if fail_at is not None and offset == fail_at:
            raise requests.ConnectionError("Steam unreachable")
        page = items[offset:offset + page_size]
        return {"reviews": page, "cursor": str(offset + len(page)),
                "total_matching": len(items) if total is None else total}
    return fetch, calls


def comment_api(n, total=None, page_size=15):
    thread = [DemoComment(str(9000 + i), 100 + i, BASE + (i + 1) * 600, None, f"comment {i}") for i in range(n)]
    return lambda thread_id, page: render_comment_page(thread[(page - 1) * page_size:page * page_size],
                                                       len(thread) if total is None else total)


# --- Reviews ---------------------------------------------------------------------------------------

def test_reviews_are_paged_to_the_end_and_checked_against_steams_total(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "review")
    fetch, calls = review_api(reviews(5))
    assert collect_reviews(conn, source, TEST_APP, start, end, fetch, **NO_LIMITS) == "complete"
    assert calls == ["*", "2", "4", "5"]
    assert source_state(conn, source)[:3] == ("complete", 5, 5)
    assert members(conn, run_id) == 5


def test_interrupted_review_collection_resumes_at_the_checkpoint_without_duplicates(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "review")
    failing, _ = review_api(reviews(5), fail_at=2)
    assert collect_reviews(conn, source, TEST_APP, start, end, failing, **NO_LIMITS) == "partial"
    assert source_state(conn, source)[:4] == ("partial", 2, 5, "2")

    working, calls = review_api(reviews(5))
    assert collect_reviews(conn, source, TEST_APP, start, end, working, **NO_LIMITS) == "complete"
    assert calls[0] == "2"  # resumed, not restarted
    assert members(conn, run_id) == 5
    assert conn.execute("SELECT count(*) FROM feedback_items WHERE app_id = %s", (TEST_APP,)).fetchone()[0] == 5


def test_a_repeated_page_stops_paging_and_is_not_called_complete(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "review")
    stuck = lambda cursor, date_range: {"reviews": reviews(5)[:2], "cursor": "same", "total_matching": 5}
    assert collect_reviews(conn, source, TEST_APP, start, end, stuck, **NO_LIMITS) == "partial"
    assert "2 of 5" in source_state(conn, source)[5]


def test_unreachable_reviews_are_unavailable_rather_than_zero(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "review")
    fetch, _ = review_api(reviews(5), fail_at=0)
    assert collect_reviews(conn, source, TEST_APP, start, end, fetch, **NO_LIMITS) == "unavailable"


def test_a_cap_leaves_the_source_partial_and_marked_capped(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "review")
    fetch, _ = review_api(reviews(5))
    limits = {**NO_LIMITS, "max_items": 3}
    assert collect_reviews(conn, source, TEST_APP, start, end, fetch, **limits) == "partial"
    status, items, _, _, capped, _ = source_state(conn, source)
    assert (items, capped) == (3, True)


def test_later_runs_reuse_the_identity_and_add_a_version_only_when_the_text_changes(ctx, tmp_path):
    conn, run_id, update_id, start, end = ctx

    def counts():
        return conn.execute("""SELECT count(DISTINCT fi.feedback_id), count(fv.version_id) FROM feedback_items fi
                               JOIN feedback_versions fv USING (feedback_id) WHERE fi.app_id = %s""",
                            (TEST_APP,)).fetchone()

    for text, expected in [("Good update", (2, 2)), ("Good update", (2, 2)), ("Edited text", (2, 4))]:
        run = create_run(conn, TEST_APP, [update_id], None, tmp_path / "x", quick_analysis())
        collect_reviews(conn, new_source(conn, run, update_id, "review"), TEST_APP, start, end,
                        review_api(reviews(2, text))[0], **NO_LIMITS)
        assert counts() == expected


# --- Comments (AC06 with synthetic pages) ---------------------------------------------------------

def test_comment_pages_are_read_until_an_empty_page_and_verified(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "comment", "test-c1")
    status = collect_comments(conn, source, TEST_APP, "thread", "test-c1", comment_api(20), **NO_LIMITS)
    assert status == "complete"
    assert source_state(conn, source)[:3] == ("complete", 20, 20)
    stored = conn.execute("SELECT count(*), count(DISTINCT author_id), min(permalink) FROM feedback_items"
                          " WHERE app_id = %s AND source_type = 'comment'", (TEST_APP,)).fetchone()
    assert stored[:2] == (20, 20) and "?ctp=1#c9000" in stored[2]


def test_fewer_comments_than_steam_reports_is_partial(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "comment", "test-c1")
    assert collect_comments(conn, source, TEST_APP, "thread", "test-c1", comment_api(20, total=25),
                            **NO_LIMITS) == "partial"
    assert "20 of 25" in source_state(conn, source)[5]


def test_an_unreadable_page_cannot_masquerade_as_zero_comments(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "comment", "test-c1")
    blocked = lambda thread_id, page: "<html><body>Please sign in</body></html>"  # no comments, no total
    assert collect_comments(conn, source, TEST_APP, "thread", "test-c1", blocked, **NO_LIMITS) == "partial"
    assert "no total" in source_state(conn, source)[5]


def test_a_thread_that_cannot_be_fetched_is_unavailable(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "comment", "test-c1")

    def forbidden(thread_id, page):
        raise requests.HTTPError("403 Forbidden")
    assert collect_comments(conn, source, TEST_APP, "thread", "test-c1", forbidden, **NO_LIMITS) == "unavailable"


def test_an_empty_thread_with_a_reported_zero_is_complete(ctx):
    conn, run_id, update_id, start, end = ctx
    source = new_source(conn, run_id, update_id, "comment", "test-c1")
    assert collect_comments(conn, source, TEST_APP, "thread", "test-c1", comment_api(0), **NO_LIMITS) == "complete"

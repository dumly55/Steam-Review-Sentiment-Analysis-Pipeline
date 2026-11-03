"""Integration tests for catalog storage and frozen runs, against the local PostgreSQL in .env.

They use a fake app id and synthetic events, delete everything they create, and are skipped when
PostgreSQL is unreachable.
"""

import time
from datetime import datetime, timezone

import psycopg
import pytest
from db_helpers import DATABASE_URL, cleanup_app, event, pager, postgres_reachable

from steam_analyzer.catalog_store import load_catalog, refresh_catalog
from steam_analyzer.db import init_db
from steam_analyzer.runs import RunError, create_run

TEST_APP = 999_000_001
DAY = 86_400
BASE = int(time.time()) - 30 * DAY

pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="local PostgreSQL not reachable")


def failing_pager(anchor):
    raise ConnectionError("Steam unreachable")


TIMELINE = [
    event("u1", BASE, "Update 1.0.0", 14),
    event("u2", BASE + 10 * DAY, "Update 1.1.0", 13),
    event("sale", BASE + 11 * DAY, "Weekend sale!", 28),
    event("hotfix", BASE + 12 * DAY, "Hotfix 1.1.1", 12),
]


@pytest.fixture
def conn():
    init_db(DATABASE_URL)
    with psycopg.connect(DATABASE_URL) as c:
        cleanup_app(c, TEST_APP)
        c.execute("INSERT INTO games (app_id, name) VALUES (%s, 'Test game')", (TEST_APP,))
        c.commit()
        yield c
        cleanup_app(c, TEST_APP)


@pytest.fixture
def no_overrides(tmp_path):
    return tmp_path / "no_overrides.json"


def run_rows(conn, run_id):
    return {title: row for title, *row in conn.execute(
        """SELECT u.title, ru.window_start, ru.window_end, ru.window_status, n.title, ru.notes_status, ru.note_version_id
           FROM run_updates ru JOIN updates u USING (update_id) LEFT JOIN updates n ON n.update_id = ru.next_update_id
           WHERE ru.run_id = %s""", (run_id,))}


def ids_by_title(conn, overrides):
    return {e.release.title: e.update_id for e in load_catalog(conn, TEST_APP, overrides).entries}


def ts(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def test_refreshing_twice_does_not_duplicate_anything(conn, no_overrides):
    def counts():
        return conn.execute(
            """SELECT (SELECT count(*) FROM source_events WHERE app_id = %(a)s),
                      (SELECT count(*) FROM update_note_versions WHERE event_gid LIKE 'test-%%'),
                      (SELECT count(*) FROM updates WHERE app_id = %(a)s)""", {"a": TEST_APP}).fetchone()

    assert refresh_catalog(conn, TEST_APP, pager(TIMELINE)).status == "complete"
    load_catalog(conn, TEST_APP, no_overrides)
    first = counts()
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))
    load_catalog(conn, TEST_APP, no_overrides)
    assert counts() == first == (4, 4, 3)


def test_selected_update_ends_at_an_unselected_hotfix(conn, no_overrides):
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))
    ids = ids_by_title(conn, no_overrides)
    run_id = create_run(conn, TEST_APP, [ids["Update 1.1.0"]],
                        lambda: refresh_catalog(conn, TEST_APP, pager(TIMELINE)), no_overrides)
    rows = run_rows(conn, run_id)
    assert list(rows) == ["Update 1.1.0"]  # only the selected update is in the run
    start, end, status, next_title, *_ = rows["Update 1.1.0"]
    assert (start, end, status, next_title) == (ts(BASE + 10 * DAY), ts(BASE + 12 * DAY), "closed", "Hotfix 1.1.1")


def test_latest_update_ends_at_the_captured_cutoff(conn, no_overrides):
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))
    ids = ids_by_title(conn, no_overrides)
    run_id = create_run(conn, TEST_APP, [ids["Hotfix 1.1.1"]],
                        lambda: refresh_catalog(conn, TEST_APP, pager(TIMELINE)), no_overrides)
    cutoff = conn.execute("SELECT cutoff_at FROM runs WHERE run_id = %s", (run_id,)).fetchone()[0]
    _, end, status, next_title, *_ = run_rows(conn, run_id)["Hotfix 1.1.1"]
    assert (end, status, next_title) == (cutoff, "ongoing", None)


def test_failed_refresh_leaves_the_latest_window_uncertain(conn, no_overrides):
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))
    conn.execute("UPDATE catalog_refreshes SET started_at = started_at - interval '1 hour' WHERE app_id = %s", (TEST_APP,))
    conn.commit()  # the last successful check was an hour before this run
    ids = ids_by_title(conn, no_overrides)
    run_id = create_run(conn, TEST_APP, [ids["Update 1.1.0"], ids["Hotfix 1.1.1"]],
                        lambda: refresh_catalog(conn, TEST_APP, failing_pager), no_overrides)
    rows = run_rows(conn, run_id)
    assert rows["Hotfix 1.1.1"][2] == "uncertain"
    assert rows["Update 1.1.0"][2] == "closed"


def test_a_new_release_does_not_change_an_existing_run(conn, no_overrides):
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))
    hotfix = ids_by_title(conn, no_overrides)["Hotfix 1.1.1"]
    first = create_run(conn, TEST_APP, [hotfix], lambda: refresh_catalog(conn, TEST_APP, pager(TIMELINE)), no_overrides)
    frozen = run_rows(conn, first)

    newer = TIMELINE + [event("u3", int(time.time()) - 60, "Update 1.2.0", 13)]
    second = create_run(conn, TEST_APP, [hotfix], lambda: refresh_catalog(conn, TEST_APP, pager(newer)), no_overrides)

    assert run_rows(conn, first) == frozen
    assert run_rows(conn, second)["Hotfix 1.1.1"][1] == ts(newer[-1]["rtime32_start_time"])


def test_notes_edited_after_the_window_use_the_archived_copy_when_one_exists(conn, no_overrides):
    edited_late = BASE + 20 * DAY  # after Update 1.1.0 replaced Update 1.0.0
    refresh_catalog(conn, TEST_APP, pager(TIMELINE))  # captures the original notes
    later = [event("u1", BASE, "Update 1.0.0", 14, body="notes + later changes", updatetime=edited_late)] + TIMELINE[1:]
    ids = ids_by_title(conn, no_overrides)
    run_id = create_run(conn, TEST_APP, [ids["Update 1.0.0"]],
                        lambda: refresh_catalog(conn, TEST_APP, pager(later)), no_overrides)
    *_, notes_status, note_id = run_rows(conn, run_id)["Update 1.0.0"]
    body = conn.execute("SELECT body FROM update_note_versions WHERE note_version_id = %s", (note_id,)).fetchone()[0]
    assert (notes_status, body) == ("unedited", "notes")


def test_notes_edited_after_the_window_without_an_archive_are_flagged(conn, no_overrides):
    later = [event("u1", BASE, "Update 1.0.0", 14, updatetime=BASE + 20 * DAY)] + TIMELINE[1:]
    refresh_catalog(conn, TEST_APP, pager(later))
    ids = ids_by_title(conn, no_overrides)
    run_id = create_run(conn, TEST_APP, [ids["Update 1.0.0"]],
                        lambda: refresh_catalog(conn, TEST_APP, pager(later)), no_overrides)
    assert run_rows(conn, run_id)["Update 1.0.0"][4] == "edited_after_window"


def test_a_run_needs_at_least_one_update(conn, no_overrides):
    with pytest.raises(RunError):
        create_run(conn, TEST_APP, [], None, no_overrides)

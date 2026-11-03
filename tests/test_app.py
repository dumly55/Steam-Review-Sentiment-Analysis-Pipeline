"""AC02: selecting updates works and never starts collection or analysis.

Runs the real Streamlit page against a synthetic catalog. Anything that would contact Steam or
create a run is replaced with a function that fails the test if called.
"""

import time
from pathlib import Path

import psycopg
import pytest
from db_helpers import DATABASE_URL, cleanup_app, event, pager, postgres_reachable
from streamlit.testing.v1 import AppTest

from steam_analyzer import catalog_store, runs
from steam_analyzer.db import init_db

APP_FILE = str(Path(__file__).resolve().parent.parent / "steam_analyzer" / "app.py")
TEST_APP = 999_000_002
DAY = 86_400

pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="local PostgreSQL not reachable")


def must_not_be_called(*args, **kwargs):
    raise AssertionError("changing the selection must not contact Steam or start a run")


@pytest.fixture
def app(monkeypatch):
    init_db(DATABASE_URL)
    base = int(time.time()) - 30 * DAY
    with psycopg.connect(DATABASE_URL) as conn:
        cleanup_app(conn, TEST_APP)
        conn.execute("INSERT INTO games (app_id, name) VALUES (%s, 'Test game')", (TEST_APP,))
        conn.commit()
        catalog_store.refresh_catalog(conn, TEST_APP, pager([
            event("app1", base, "Update 1.0.0", 14),
            event("app2", base + 10 * DAY, "Update 1.1.0", 13),
            event("app3", base + 12 * DAY, "Hotfix 1.1.1"),
        ]))
        monkeypatch.setenv("STEAM_APP_ID", str(TEST_APP))
        monkeypatch.setattr(catalog_store, "live_refresh", must_not_be_called)
        monkeypatch.setattr(runs, "create_run", must_not_be_called)
        yield AppTest.from_file(APP_FILE, default_timeout=60).run(), conn
        cleanup_app(conn, TEST_APP)


def selected_text(at) -> str:
    return next(m.value for m in at.markdown if "updates selected" in m.value)


def test_select_all_and_clear_without_starting_anything(app):
    at, conn = app
    assert not at.exception
    assert "**0 of 3**" in selected_text(at)
    assert at.button(key="run").disabled  # nothing selected

    at.button(key="select_all").click().run()
    assert not at.exception
    assert "**3 of 3**" in selected_text(at) and "every update in the loaded catalog" in selected_text(at)
    assert not at.button(key="run").disabled

    at.button(key="clear").click().run()
    assert not at.exception
    assert "**0 of 3**" in selected_text(at)
    assert at.button(key="run").disabled

    assert conn.execute("SELECT count(*) FROM runs WHERE app_id = %s", (TEST_APP,)).fetchone()[0] == 0

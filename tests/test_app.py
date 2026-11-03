"""AC02: selecting updates works and never starts collection or analysis.

Runs the real Streamlit page against a synthetic catalog. Anything that would contact Steam or
create a run is replaced with a function that fails the test if called.
"""

import time
from pathlib import Path

import psycopg
import pytest
from db_helpers import DATABASE_URL, cleanup_app, event, pager, postgres_reachable, quick_analysis
from streamlit.testing.v1 import AppTest

from steam_analyzer import catalog_store, pipeline, runs
from steam_analyzer.collectors import Sources
from steam_analyzer.db import init_db
from steam_analyzer.demo import ScriptedExtractor, render_comment_page

real_create_run = runs.create_run  # kept before the fixture replaces it

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
        monkeypatch.setattr(pipeline, "launch_worker", must_not_be_called)
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


def test_results_preview_shows_labels_counts_and_denominators(app):
    at, conn = app
    update_id = conn.execute("SELECT update_id FROM updates WHERE app_id = %s AND title = 'Update 1.1.0'",
                             (TEST_APP,)).fetchone()[0]
    run_id = real_create_run(conn, TEST_APP, [update_id], None, analysis=quick_analysis())
    start = int(conn.execute("SELECT extract(epoch FROM window_start) FROM run_updates WHERE run_id = %s",
                             (run_id,)).fetchone()[0])
    review = {"recommendationid": "1", "author": {"steamid": "76561198000000001"}, "language": "english",
              "review": "The new update broke matchmaking.", "timestamp_created": start + 60,
              "timestamp_updated": start + 60, "voted_up": False}
    answers = {"review:1": {"feedback_id": "review:1", "overall_sentiment": "negative",
                            "update_relevance": "explicit_update", "uncertainty_reason": "",
                            "observations": [{"feature_key": "matchmaking", "category": "matchmaking",
                                              "sentiment": "negative", "relevance": "explicit_update",
                                              "evidence": "The new update broke matchmaking", "change_id": ""}]}}
    sources = Sources(lambda cursor, date_range: {"reviews": [review] if cursor == "*" else [], "cursor": "1",
                                                  "total_matching": 1},
                      lambda thread_id, page: render_comment_page([], 0))
    assert pipeline.process_run(conn, run_id, sources, lambda cfg: ScriptedExtractor(answers, 4000)) == "completed"

    at.run()
    assert not at.exception
    assert f"Run #{run_id} · completed" in [s.value for s in at.subheader]
    markdown = " ".join(m.value for m in at.markdown)
    assert "Overall reception among analyzed authors: Insufficient feedback" in markdown
    reasons = next(d.value for d in at.dataframe if "Feature" in d.value.columns)
    assert reasons.iloc[0]["Authors"] == "1 of 1 analyzed authors (100%)"
    assert reasons.iloc[0]["Example"] == "The new update broke matchmaking"

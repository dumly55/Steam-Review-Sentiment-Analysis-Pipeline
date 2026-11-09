"""End-to-end processing of a run against the local PostgreSQL: synthetic pages, scripted model answers,
and the real collectors, eligibility rules, cache and SQL views.

Uses the fake app id 999000001, deletes everything it creates, and is skipped without PostgreSQL.
Small label thresholds are set in the run config so a handful of synthetic authors can produce labels.
"""

import time

import psycopg
import pytest
import requests
from db_helpers import DATABASE_URL, cleanup_app, event, pager, postgres_reachable, quick_analysis

from steam_analyzer.catalog_store import load_catalog, refresh_catalog
from steam_analyzer.collectors import Sources
from steam_analyzer.db import init_db
from steam_analyzer.demo import DemoComment, ScriptedExtractor, render_comment_page
from steam_analyzer.extraction import ExtractorUnavailable
from steam_analyzer.pipeline import process_run
from steam_analyzer.runs import create_run
from steam_analyzer.steam_sources import STEAMID64_BASE

TEST_APP = 999_000_001
DAY, HOUR = 86_400, 3_600
BASE = int(time.time()) - 30 * DAY
START, END = BASE + 10 * DAY, BASE + 12 * DAY  # Update 1.1.0's window
NOTES = "[h3]Balance[/h3][list][*][p]Scope zoom reduced.[/p][/*][*][p]Fixed a crash on extraction.[/p][/*][/list]"
TIMELINE = [
    event("u1", BASE, "Update 1.0.0", 14),
    event("u2", START, "Update 1.1.0", 13, body=NOTES),
    event("sale", BASE + 11 * DAY, "Weekend sale!", 28),  # not a boundary
    event("u3", END, "Hotfix 1.1.1", 12),
]
THRESHOLDS = dict(label_min_qualifying_authors=3, recurring_min_authors=2)

pytestmark = pytest.mark.skipif(not postgres_reachable(), reason="local PostgreSQL not reachable")


def o(key, sentiment, evidence, change="C1", relevance="explicit_update", category="balance"):
    return {"feature_key": key, "category": category, "sentiment": sentiment, "relevance": relevance,
            "evidence": evidence, "change_id": change}


class Scenario:
    """Synthetic reviews and comments for Update 1.1.0 plus the scripted answer for each one."""

    def __init__(self):
        self.reviews, self.comments, self.answers = [], [], {}
        self.review("A", "Love the game, but the scope nerf is awful.", True, overall="positive",
                    obs=[o("scope", "negative", "the scope nerf is awful"),
                         o("game", "positive", "Love the game", "", "general_game", "other")])  # AC20
        self.review("B", "Scope nerf ruined it.", False, obs=[o("scope", "negative", "Scope nerf ruined it")])
        self.review("M", "The scope got nerfed hard.", False, obs=[o("Scope", "negative", "The scope got nerfed hard")])
        self.review("C", "Game crashes on extraction are fixed.", True, overall="positive",
                    obs=[o("game_crashes", "positive", "Game crashes on extraction are fixed", "C2", category="stability")])
        self.review("L", "No more crashes, nice.", True, overall="positive",
                    obs=[o("crashes", "positive", "No more crashes", "C2", category="stability")])
        self.review("F", "10/10", True, obs=[o("game", "positive", "best game ever", "")])  # never a valid quote
        self.review("D", "Scope is fine.", True, updated=END + DAY, obs=[o("scope", "neutral", "Scope is fine")])
        self.review("E", "Posted at the hotfix release.", True, created=END)
        self.review("P", "Posted before the release.", True, created=START - 1)

        for i in range(11):  # one author, eleven comments about the same change (AC08)
            self.comment(1, f"Undo the scope nerf, day {i}.", [o("scope", "negative", "Undo the scope nerf")])
        self.comment(2, "The scope change is great and terrible at once.",
                     [o("scope", "mixed", "The scope change is great and terrible at once")])
        self.comment(3, "The new scope is great.", [o("scope", "positive", "The new scope is great")])
        self.comment(3, "Actually the scope is bad.", [o("scope", "negative", "the scope is bad")])
        self.comment(None, "Good patch.", [o("update_overall", "positive", "Good patch", "", category="other")])
        self.comment(4, "Any news on the next patch?", [], created=END + HOUR)  # AC07: after the window

    def review(self, rid, text, voted_up, created=None, updated=None, overall="negative", obs=()):
        created = created or START + (len(self.reviews) + 1) * HOUR
        self.reviews.append({"recommendationid": rid, "author": {"steamid": f"7656119800000{len(self.reviews):04d}"},
                             "language": "english", "review": text, "timestamp_created": created,
                             "timestamp_updated": updated or created, "voted_up": voted_up})
        self.answers[f"review:{rid}"] = {"feedback_id": f"review:{rid}", "overall_sentiment": overall,
                                         "update_relevance": "explicit_update", "observations": list(obs),
                                         "uncertainty_reason": ""}

    def comment(self, account, text, obs, created=None):
        cid = str(50_000 + len(self.comments))
        created = created or START + (len(self.comments) + 1) * 600
        self.comments.append(DemoComment(cid, account, created, None, text))
        self.answers[f"comment:{cid}"] = {"feedback_id": f"comment:{cid}", "overall_sentiment": "neutral",
                                          "update_relevance": "explicit_update", "observations": obs,
                                          "uncertainty_reason": ""}

    def sources(self) -> Sources:
        def fetch_reviews(cursor, date_range):  # ignores the date range, so the local window check must work
            offset = 0 if cursor == "*" else int(cursor)
            page = self.reviews[offset:offset + 4]
            return {"reviews": page, "cursor": str(offset + len(page)), "total_matching": len(self.reviews)}

        def fetch_comments(thread_id, page):
            assert thread_id == "test-tu2"  # the thread linked from Update 1.1.0's own announcement
            return render_comment_page(self.comments[(page - 1) * 15:page * 15], len(self.comments))
        return Sources(fetch_reviews, fetch_comments)

    def extractor(self, config):
        return ScriptedExtractor(self.answers, config["analysis"]["extraction"]["max_feedback_chars"])


@pytest.fixture
def setup(tmp_path):
    init_db(DATABASE_URL)
    with psycopg.connect(DATABASE_URL) as conn:
        cleanup_app(conn, TEST_APP)
        conn.execute("INSERT INTO games (app_id, name) VALUES (%s, 'Test game')", (TEST_APP,))
        conn.commit()
        refresh_catalog(conn, TEST_APP, pager(TIMELINE))
        ids = {e.release.title: e.update_id for e in load_catalog(conn, TEST_APP, tmp_path / "x").entries}

        def new_run(**thresholds):
            return create_run(conn, TEST_APP, [ids["Update 1.1.0"]], None, tmp_path / "x",
                              quick_analysis(**{**THRESHOLDS, **thresholds}))
        yield conn, Scenario(), new_run, ids["Update 1.1.0"]
        cleanup_app(conn, TEST_APP)


def source(conn, run_id, source_type):
    cur = conn.execute("SELECT * FROM v_source_reception WHERE run_id = %s AND source_type = %s", (run_id, source_type))
    return dict(zip([c.name for c in cur.description], cur.fetchone()))


def feature(conn, run_id, source_type, key, tier="patch_related"):
    cur = conn.execute("SELECT * FROM v_feature_reasons WHERE run_id = %s AND source_type = %s AND feature_key = %s"
                       " AND tier = %s", (run_id, source_type, key, tier))
    row = cur.fetchone()
    return dict(zip([c.name for c in cur.description], row)) if row else None


def overall(conn, run_id):
    return conn.execute("SELECT overall_label, basis FROM v_update_reception WHERE run_id = %s", (run_id,)).fetchone()


def exclusions(conn, run_id, source_type):
    return dict(conn.execute("""SELECT source_item_id, exclusion_reason FROM run_feedback
                                WHERE run_id = %s AND source_type = %s AND eligibility = 'excluded'""",
                             (run_id, source_type)).fetchall())


@pytest.fixture
def processed(setup):
    conn, scenario, new_run, update_id = setup
    run_id = new_run()
    assert process_run(conn, run_id, scenario.sources(), scenario.extractor) == "completed"
    return conn, scenario, new_run, run_id


def test_window_and_edit_exclusions(processed):
    conn, scenario, _, run_id = processed
    assert exclusions(conn, run_id, "review") == {"D": "edited_after_window",  # AC13: no earlier text captured
                                                   "E": "outside_window",       # created at the next release
                                                   "P": "outside_window"}
    late = scenario.comments[-1].comment_id
    assert exclusions(conn, run_id, "comment") == {late: "outside_window"}  # AC07


def test_failed_analysis_stays_in_the_coverage_denominator(processed):  # AC21
    conn, _, _, run_id = processed
    r = source(conn, run_id, "review")
    assert (r["eligible"], r["analyzed"], r["failed"], float(r["coverage"])) == (6, 5, 1, 0.8333)
    assert (r["label"], r["insufficient_reason"]) == ("Insufficient feedback", "analysis coverage below 90%")


def test_one_author_posting_many_times_counts_once_and_mixed_counts_once(processed):  # AC08
    conn, _, _, run_id = processed
    c = source(conn, run_id, "comment")
    assert (c["eligible"], c["analyzed"], c["analyzed_authors"], c["unknown_author_items"]) == (15, 15, 3, 1)
    assert (c["positive_authors"], c["negative_authors"], c["mixed_authors"]) == (0, 1, 2)
    scope = feature(conn, run_id, "comment", "scope")
    assert (scope["authors"], scope["negative_authors"], scope["mixed_authors"], scope["observations"]) == (3, 1, 2, 14)
    assert scope["support"] == "recurring_mixed"  # popular but divided


def test_general_praise_with_patch_criticism_is_a_negative_patch_reaction(processed):  # AC20
    conn, _, _, run_id = processed
    reaction = conn.execute("""SELECT reaction FROM v_author_reactions ar
                               JOIN feedback_items fi ON fi.author_id = ar.author_id
                               WHERE ar.run_id = %s AND fi.source_item_id = 'A'""", (run_id,)).fetchone()[0]
    assert reaction == "negative"
    assert feature(conn, run_id, "review", "game", "general")["positive_authors"] == 1  # kept, but not the verdict


def test_recommendation_text_sentiment_and_patch_reception_are_separate(processed):  # AC11
    conn, _, _, run_id = processed
    r = source(conn, run_id, "review")
    assert (r["recommended"], r["with_recommendation"]) == (4, 6)    # all eligible reviews, AI or not
    assert (r["text_positive"], r["text_negative"]) == (3, 2)        # overall tone: A reads as positive...
    assert (r["positive_authors"], r["negative_authors"]) == (2, 3)  # ...but A's patch reaction is negative


def test_each_reason_has_count_denominator_share_support_and_evidence(processed):  # AC10, AC09
    conn, _, _, run_id = processed
    scope = feature(conn, run_id, "review", "scope")  # "Scope" and "scope" are one feature
    assert (scope["authors"], scope["analyzed_authors"], float(scope["share"])) == (3, 5, 0.6)
    assert scope["support"] == "recurring_criticism" and scope["change_id"] == "C1"
    assert scope["negative_example"] == "the scope nerf is awful" and scope["negative_example_link"]
    crashes = feature(conn, run_id, "review", "crashes")  # "game_crashes" maps to "crashes" via the alias list
    assert (crashes["authors"], crashes["positive_authors"], crashes["support"]) == (2, 2, "recurring_praise")
    assert feature(conn, run_id, "review", "game_crashes") is None


def test_labels_follow_the_frozen_thresholds_and_combine_sources(processed):  # AC12
    conn, _, _, run_id = processed
    assert source(conn, run_id, "comment")["label"] == "Mixed"
    assert overall(conn, run_id) == ("Mixed", "provisional, comments only")

    # Lower the coverage threshold in this run's frozen config: re-aggregation only, no new inference.
    conn.execute("""UPDATE runs SET config = jsonb_set(config, '{analysis,thresholds,label_min_coverage}', '0.8')
                    WHERE run_id = %s""", (run_id,))
    assert source(conn, run_id, "review")["label"] == "Negative"  # 3 of 5 negative: exactly 60% and a 20% margin
    assert overall(conn, run_id) == ("Mixed", "both sources")     # Negative vs Mixed disagree

    conn.execute("UPDATE run_sources SET status = 'partial' WHERE run_id = %s AND source_type = 'comment'", (run_id,))
    assert source(conn, run_id, "comment")["insufficient_reason"] == "collection partial"
    assert overall(conn, run_id) == ("Negative", "provisional, reviews only")
    conn.rollback()


def test_repeating_the_run_reuses_every_assessment_and_counts_the_same(processed):  # AC14
    conn, scenario, new_run, first = processed
    assessments = conn.execute("SELECT count(*) FROM ai_assessments WHERE app_id = %s AND status = 'succeeded'",
                               (TEST_APP,)).fetchone()[0]
    second = new_run()
    assert process_run(conn, second, scenario.sources(), scenario.extractor) == "completed"
    keys = ["collected", "eligible", "analyzed", "failed", "analyzed_authors", "positive_authors", "negative_authors",
            "mixed_authors", "label"]
    for source_type in ("review", "comment"):
        a, b = source(conn, first, source_type), source(conn, second, source_type)
        assert [a[k] for k in keys] == [b[k] for k in keys]
        assert b["cache_hits"] == b["analyzed"]
    assert conn.execute("SELECT count(*) FROM ai_assessments WHERE app_id = %s AND status = 'succeeded'",
                        (TEST_APP,)).fetchone()[0] == assessments
    assert conn.execute("SELECT count(*) FROM feedback_items WHERE app_id = %s", (TEST_APP,)).fetchone()[0] == 25


def test_a_run_stopped_by_an_unreachable_model_resumes_where_it_stopped(setup):
    conn, scenario, new_run, _ = setup
    run_id = new_run()

    def unreachable(config):
        raise ExtractorUnavailable("Ollama is not reachable. Start the Ollama app.")
    assert process_run(conn, run_id, scenario.sources(), unreachable) == "failed"
    status, error = conn.execute("SELECT status, error FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    assert status == "failed" and "Start the Ollama app" in error
    assert source(conn, run_id, "review")["pending"] == 6  # collected and checked, waiting for the model

    assert process_run(conn, run_id, scenario.sources(), scenario.extractor) == "completed"
    assert source(conn, run_id, "review")["collected"] == 9  # nothing collected twice
    assert source(conn, run_id, "review")["pending"] == 0


def test_a_model_lost_mid_run_with_parallel_requests_leaves_no_duplicates_and_resumes(setup):
    conn, scenario, new_run, _ = setup
    run_id = new_run()
    calls = []

    class DropsAfterThree(ScriptedExtractor):
        def complete(self, messages, schema):
            calls.append(1)
            if len(calls) > 3:
                raise requests.ConnectionError("connection refused")
            return super().complete(messages, schema)

    dropping = lambda config: DropsAfterThree(scenario.answers, config["analysis"]["extraction"]["max_feedback_chars"])
    assert process_run(conn, run_id, scenario.sources(), dropping) == "failed"
    stored = conn.execute("SELECT count(*) FROM run_feedback WHERE run_id = %s AND analysis_status <> 'pending'"
                          " AND eligibility = 'eligible'", (run_id,)).fetchone()[0]
    assert 0 < stored <= 3

    assert process_run(conn, run_id, scenario.sources(), scenario.extractor) == "completed"
    assert conn.execute("SELECT count(*) FROM run_feedback WHERE run_id = %s AND eligibility = 'eligible'"
                        " AND analysis_status = 'pending'", (run_id,)).fetchone()[0] == 0
    assert conn.execute("""SELECT count(*) FROM ai_assessments WHERE app_id = %s AND status = 'succeeded'
                           GROUP BY cache_key HAVING count(*) > 1""", (TEST_APP,)).fetchall() == []


def test_an_edit_after_the_window_uses_text_captured_by_an_earlier_run(setup):  # AC13
    conn, scenario, new_run, _ = setup
    first = new_run()
    process_run(conn, first, scenario.sources(), scenario.extractor)

    edited = next(r for r in scenario.reviews if r["recommendationid"] == "B")
    edited.update(review="Rewritten months later.", timestamp_updated=END + 30 * DAY)
    second = new_run()
    process_run(conn, second, scenario.sources(), scenario.extractor)
    analyzed_text, reason = conn.execute(
        """SELECT fv.text, rf.exclusion_reason FROM run_feedback rf LEFT JOIN feedback_versions fv USING (version_id)
           WHERE rf.run_id = %s AND rf.source_item_id = 'B'""", (second,)).fetchone()
    assert (analyzed_text, reason) == ("Scope nerf ruined it.", None)


def test_only_one_run_per_game_is_processed_at_a_time(setup):
    conn, scenario, new_run, _ = setup
    run_id = new_run()
    with psycopg.connect(DATABASE_URL) as other:
        other.execute("SELECT pg_advisory_lock(771, %s)", (TEST_APP,))
        with pytest.raises(Exception, match="already being processed"):
            process_run(conn, run_id, scenario.sources(), scenario.extractor)

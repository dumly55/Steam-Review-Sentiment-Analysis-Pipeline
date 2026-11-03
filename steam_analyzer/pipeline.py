"""Processing a frozen run: collect both sources, decide eligibility, analyze locally, aggregate.

Every stage can resume. Collection continues from each source's checkpoint, eligibility is decided once
per collected item, and analysis only handles items still pending, reusing cached assessments.
One run per game is processed at a time, enforced with a PostgreSQL advisory lock that is released
automatically if the worker process dies.
"""

import logging
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from steam_analyzer import steam_sources as steam
from steam_analyzer.collectors import Sources, collect_comments, collect_reviews
from steam_analyzer.config import Settings
from steam_analyzer.extraction import (
    PROMPT_VERSION, SCHEMA_VERSION, Context, Extractor, ExtractResult, OllamaExtractor, cache_key, context_sha256,
    extract,
)
from steam_analyzer.feedback import StoredVersion, check_eligibility
from steam_analyzer.http import make_session
from steam_analyzer.runs import MISSING_NOTES, RunError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "data" / "logs"
LOCK_CLASS = 771  # advisory-lock namespace: (LOCK_CLASS, app_id) is held while a run of that app is processed
ACTIVE_STATUSES = ("collecting", "analyzing", "aggregating")

MakeExtractor = Callable[[dict], Extractor]  # run config -> extractor


@dataclass(frozen=True)
class RunUpdate:
    run_id: int
    update_id: int
    title: str
    window_start: datetime | None
    window_end: datetime | None
    notes_context: str | None


def live_sources(app_id: int, page_size: int) -> Sources:
    session = make_session()
    return Sources(
        fetch_reviews=lambda cursor, date_range: steam.fetch_reviews_page(
            session, app_id, cursor=cursor, num_per_page=page_size, date_range=date_range),
        fetch_comment_page=lambda thread_id, page: steam.fetch_comment_page(session, app_id, thread_id, page),
    )


def live_extractor(settings: Settings) -> MakeExtractor:
    return lambda config: OllamaExtractor(settings.ollama_host, config["analysis"]["model"],
                                          config["analysis"]["extraction"])


def process(conn: psycopg.Connection, run_id: int, settings: Settings) -> str:
    """Process a run with live Steam and Ollama, or with synthetic data if it belongs to the demo game."""
    from steam_analyzer import demo

    row = conn.execute("SELECT app_id, config FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    if row is None:
        raise RunError(f"Run {run_id} does not exist.")
    app_id, config = row
    if app_id == demo.DEMO_APP_ID:
        return process_run(conn, run_id, demo.demo_sources(), demo.make_scripted_extractor)
    page_size = config["analysis"]["collection"]["review_page_size"]
    return process_run(conn, run_id, live_sources(app_id, page_size), live_extractor(settings))


def process_run(conn: psycopg.Connection, run_id: int, sources: Sources, make_extractor: MakeExtractor,
                sleep: Callable[[float], None] = time.sleep) -> str:
    """Run (or resume) every stage and return the final status."""
    row = conn.execute("SELECT app_id, status, config FROM runs WHERE run_id = %s", (run_id,)).fetchone()
    if row is None:
        raise RunError(f"Run {run_id} does not exist.")
    app_id, status, config = row
    if status in ("completed", "cancelled"):
        return status
    if not conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASS, app_id)).fetchone()[0]:
        conn.rollback()
        raise RunError("Another run for this game is already being processed.")
    conn.commit()
    analysis = config["analysis"]
    try:
        _set_status(conn, run_id, "collecting")
        updates = _load_run_updates(conn, run_id)
        for ru in updates:
            _collect(conn, app_id, ru, sources, analysis, sleep)
            _decide_eligibility(conn, ru)
        _set_status(conn, run_id, "analyzing")
        extractor = make_extractor(config)
        for ru in updates:
            _analyze(conn, app_id, ru, analysis, extractor)
        _set_status(conn, run_id, "aggregating")
        final = _final_status(conn, run_id)
        _log_results(conn, run_id)
        _set_status(conn, run_id, final, finished=True)
        return final
    except Exception as exc:
        conn.rollback()
        log.exception("run %d failed", run_id)
        _set_status(conn, run_id, "failed", finished=True, error=str(exc))
        return "failed"
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_CLASS, app_id))
        conn.commit()


def worker_active(conn: psycopg.Connection, app_id: int) -> bool:
    """True while some process holds the processing lock for this game."""
    return conn.execute(
        """SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted
                          AND classid = %s::bigint::oid AND objid = %s::bigint::oid AND objsubid = 2)""",
        (LOCK_CLASS, app_id)).fetchone()[0]


def launch_worker(run_id: int) -> Path:
    """Start `python -m steam_analyzer process <run_id>` in the background; its output goes to a log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{run_id}.log"
    with open(log_path, "a", encoding="utf-8") as out:
        subprocess.Popen(
            [sys.executable, "-m", "steam_analyzer", "process", str(run_id)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return log_path


# --- Stages --------------------------------------------------------------------------------------

def _set_status(conn: psycopg.Connection, run_id: int, status: str, finished: bool = False,
                error: str | None = None) -> None:
    conn.execute(
        """UPDATE runs SET status = %s, started_at = COALESCE(started_at, now()),
                  finished_at = CASE WHEN %s THEN now() END, error = %s
           WHERE run_id = %s""",
        (status, finished, error, run_id))
    conn.commit()
    log.info("run %d: %s%s", run_id, status, f" ({error})" if error else "")


def _load_run_updates(conn: psycopg.Connection, run_id: int) -> list[RunUpdate]:
    rows = conn.execute(
        """SELECT ru.run_id, ru.update_id, u.title, ru.window_start, ru.window_end, ru.notes_context
           FROM run_updates ru JOIN updates u USING (update_id)
           WHERE ru.run_id = %s ORDER BY ru.window_start NULLS LAST, ru.update_id""", (run_id,)).fetchall()
    return [RunUpdate(*row) for row in rows]


def _source_row(conn: psycopg.Connection, ru: RunUpdate, source_type: str, event_gid: str | None,
                thread_id: str | None, status: str = "pending", detail: str = "") -> int:
    conn.execute(
        """INSERT INTO run_sources (run_id, update_id, source_type, event_gid, thread_id, status, detail)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (ru.run_id, ru.update_id, source_type, event_gid, thread_id, status, detail))
    conn.commit()
    return conn.execute(
        """SELECT run_source_id FROM run_sources WHERE run_id = %s AND update_id = %s AND source_type = %s
           AND event_gid IS NOT DISTINCT FROM %s""", (ru.run_id, ru.update_id, source_type, event_gid)).fetchone()[0]


def _collect(conn: psycopg.Connection, app_id: int, ru: RunUpdate, sources: Sources, analysis: dict,
             sleep: Callable[[float], None]) -> None:
    if ru.window_start is None or ru.window_end is None:
        for source_type in ("review", "comment"):
            _source_row(conn, ru, source_type, None, None, "unavailable",
                        "Not collected: this update's window has no verified start or end.")
        return
    settings = analysis["collection"]
    limits = dict(max_items=analysis.get("max_items_per_source"), max_pages=settings["max_pages"],
                  page_delay=settings["page_delay_seconds"], sleep=sleep)

    source_id = _source_row(conn, ru, "review", None, None)
    collect_reviews(conn, source_id, app_id, ru.window_start, ru.window_end, sources.fetch_reviews, **limits)

    # Comments live on each announcement post about this release (the primary post and any duplicates).
    posts = conn.execute(
        """SELECT us.event_gid, se.forum_topic_id FROM update_sources us JOIN source_events se USING (event_gid)
           WHERE us.update_id = %s ORDER BY us.role DESC, us.event_gid""", (ru.update_id,)).fetchall()
    for event_gid, thread_id in posts:
        if thread_id is None:
            _source_row(conn, ru, "comment", event_gid, None, "unavailable",
                        "This announcement post has no linked comment thread.")
            continue
        source_id = _source_row(conn, ru, "comment", event_gid, thread_id)
        collect_comments(conn, source_id, app_id, thread_id, event_gid, sources.fetch_comment_page, **limits)


def _decide_eligibility(conn: psycopg.Connection, ru: RunUpdate) -> None:
    """Apply the window, historical-edit, language and text checks to every newly collected item."""
    rows = conn.execute(
        """SELECT rf.source_type, rf.source_item_id, fi.created_at, fv.version_id, fv.edited_at, fv.text, fv.language
           FROM run_feedback rf
           JOIN feedback_items fi ON fi.feedback_id = rf.feedback_id
           JOIN feedback_versions fv ON fv.feedback_id = rf.feedback_id
           WHERE rf.run_id = %s AND rf.update_id = %s AND rf.eligibility = 'pending'""",
        (ru.run_id, ru.update_id)).fetchall()
    items: dict[tuple[str, str], tuple[datetime, list[StoredVersion]]] = {}
    for source_type, item_id, created_at, *version in rows:
        items.setdefault((source_type, item_id), (created_at, []))[1].append(StoredVersion(*version))
    decisions = []
    for (source_type, item_id), (created_at, versions) in items.items():
        version, reason = check_eligibility(created_at, versions, ru.window_start, ru.window_end)
        decisions.append((version.version_id if version else None, "excluded" if reason else "eligible", reason,
                          "not_applicable" if reason else "pending", ru.run_id, ru.update_id, source_type, item_id))
    conn.cursor().executemany(
        """UPDATE run_feedback SET version_id = %s, eligibility = %s, exclusion_reason = %s, analysis_status = %s
           WHERE run_id = %s AND update_id = %s AND source_type = %s AND source_item_id = %s""", decisions)
    conn.commit()


def _analyze(conn: psycopg.Connection, app_id: int, ru: RunUpdate, analysis: dict, extractor: Extractor) -> None:
    change_ids = frozenset(r[0] for r in conn.execute(
        "SELECT change_id FROM run_note_changes WHERE run_id = %s AND update_id = %s", (ru.run_id, ru.update_id)))
    ctx = Context(ru.title, ru.notes_context or MISSING_NOTES, change_ids,
                  tuple(analysis["categories"]), tuple(analysis["known_feature_keys"]))
    ctx_hash = context_sha256(ctx)
    pending = conn.execute(
        """SELECT rf.source_type, rf.source_item_id, fv.text, fv.text_sha256
           FROM run_feedback rf
           JOIN feedback_versions fv ON fv.version_id = rf.version_id
           JOIN feedback_items fi ON fi.feedback_id = rf.feedback_id
           WHERE rf.run_id = %s AND rf.update_id = %s AND rf.eligibility = 'eligible' AND rf.analysis_status = 'pending'
           ORDER BY rf.source_type, fi.created_at, rf.source_item_id""", (ru.run_id, ru.update_id)).fetchall()
    if not pending:
        return
    log.info("update %d (%s): analyzing %d items", ru.update_id, ru.title, len(pending))
    counts: Counter[str] = Counter()
    started = time.monotonic()
    for i, (source_type, item_id, text, text_hash) in enumerate(pending, 1):
        key = cache_key(app_id, source_type, item_id, text_hash, ctx_hash, extractor.model, extractor.model_digest,
                        extractor.settings)
        hit = conn.execute("SELECT assessment_id FROM ai_assessments WHERE cache_key = %s AND status = 'succeeded'",
                           (key,)).fetchone()
        if hit:
            assessment_id, status, cached = hit[0], "succeeded", True
        else:
            result = extract(extractor, ctx, source_type, f"{source_type}:{item_id}", text)
            assessment_id = _store_assessment(conn, key, app_id, source_type, item_id, text_hash, ctx_hash,
                                              extractor, result)
            status, cached = ("succeeded" if result.extraction else "failed"), False
        conn.execute(
            """UPDATE run_feedback SET assessment_id = %s, analysis_status = %s, cache_hit = %s
               WHERE run_id = %s AND update_id = %s AND source_type = %s AND source_item_id = %s""",
            (assessment_id, status, cached, ru.run_id, ru.update_id, source_type, item_id))
        conn.commit()
        counts[status] += 1
        counts["cache hits"] += cached
        if i % 25 == 0 or i == len(pending):
            elapsed = time.monotonic() - started
            log.info("update %d: %d/%d analyzed (%s), %.1f s per item", ru.update_id, i, len(pending),
                     ", ".join(f"{v} {k}" for k, v in counts.items()), elapsed / i)


def _store_assessment(conn: psycopg.Connection, key: str, app_id: int, source_type: str, item_id: str,
                      text_hash: str, ctx_hash: str, extractor: Extractor, result: ExtractResult) -> int:
    e = result.extraction
    assessment_id = conn.execute(
        """INSERT INTO ai_assessments (cache_key, app_id, source_type, source_item_id, text_sha256, context_sha256,
                                       model, model_digest, prompt_version, schema_version, settings, status, error,
                                       attempts, overall_sentiment, update_relevance, uncertainty_reason,
                                       input_truncated, raw_output, duration_ms, output_tokens)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING assessment_id""",
        (key, app_id, source_type, item_id, text_hash, ctx_hash, extractor.model, extractor.model_digest,
         PROMPT_VERSION, SCHEMA_VERSION, Jsonb(extractor.settings), "succeeded" if e else "failed", result.error,
         result.attempts, e.overall_sentiment if e else None, e.update_relevance if e else None,
         e.uncertainty_reason if e else None, result.input_truncated, result.raw_output, result.duration_ms,
         result.output_tokens),
    ).fetchone()[0]
    if e:
        conn.cursor().executemany(
            """INSERT INTO feature_observations (assessment_id, position, feature_key, category, sentiment, relevance,
                                                 evidence, change_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [(assessment_id, i, o.feature_key, o.category, o.sentiment, o.relevance, o.evidence, o.change_id)
             for i, o in enumerate(e.observations, 1)])
    return assessment_id


def _final_status(conn: psycopg.Connection, run_id: int) -> str:
    incomplete = conn.execute("SELECT count(*) FROM run_sources WHERE run_id = %s AND status <> 'complete'",
                              (run_id,)).fetchone()[0]
    return "partial" if incomplete else "completed"


def _log_results(conn: psycopg.Connection, run_id: int) -> None:
    for title, label, basis in conn.execute(
            "SELECT title, overall_label, basis FROM v_update_reception WHERE run_id = %s ORDER BY window_start",
            (run_id,)):
        log.info("run %d: %s -> %s%s", run_id, title, label, f" ({basis})" if basis else "")

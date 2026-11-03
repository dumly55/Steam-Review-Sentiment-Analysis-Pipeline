"""Creating runs: capture the cutoff once, refresh the catalog, and freeze each selected update's window.

Collection and analysis (Phase 3) will read these frozen rows rather than recomputing windows,
so a release published mid-run cannot change a run that already started.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from steam_analyzer.catalog import NoteVersion, choose_note_version, compute_windows
from steam_analyzer.catalog_store import (
    DUPLICATE_MERGE_HOURS, OVERRIDES_FILE, RefreshResult, load_catalog,
)

WINDOW_RULE = "start <= feedback created_at < next strictly later release, or the run cutoff for the latest"


class RunError(ValueError):
    pass


def create_run(
    conn: psycopg.Connection,
    app_id: int,
    update_ids: list[int],
    refresh: Callable[[], RefreshResult] | None,
    overrides_path: Path = OVERRIDES_FILE,
) -> int:
    """Freeze a run for the selected updates and return its id. Nothing is collected or analyzed here.

    The cutoff is captured before the catalog refresh, so a successful refresh verifies the catalog
    through the cutoff. If the refresh fails, the latest window is stored as 'uncertain'.
    """
    selected = sorted(set(update_ids))
    if not selected:
        raise RunError("Select at least one update.")
    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
    refreshed = refresh() if refresh else None
    catalog = load_catalog(conn, app_id, overrides_path)

    by_id = {e.update_id: e.release for e in catalog.entries}
    unknown = [uid for uid in selected if uid not in by_id]
    if unknown:
        raise RunError(f"Not in the current catalog: {unknown}")
    windows = compute_windows(list(by_id.values()), cutoff, catalog.verified_through)
    id_by_key = {release.key: uid for uid, release in by_id.items()}

    snapshot = [
        {"update_id": e.update_id, "title": e.release.title, "version": e.release.version,
         "released_at": e.release.released_at.isoformat() if e.release.released_at else None,
         "released_at_source": e.release.released_at_source}
        for e in catalog.entries
    ]
    config = {
        "app_id": app_id,
        "window_rule": WINDOW_RULE,
        "duplicate_merge_hours": DUPLICATE_MERGE_HOURS,
        "overrides_file": overrides_path.read_text(encoding="utf-8") if overrides_path.exists() else None,
        "catalog_refresh_status": refreshed.status if refreshed else "not refreshed",
        "catalog_coverage_warning": catalog.coverage_warning,
    }

    try:
        run_id = conn.execute(
            """INSERT INTO runs (app_id, cutoff_at, status, catalog_refresh_id, config, timeline_snapshot)
               VALUES (%s, %s, 'queued', %s, %s, %s) RETURNING run_id""",
            (app_id, cutoff, refreshed.refresh_id if refreshed else catalog.refresh_id,
             Jsonb(config), Jsonb(snapshot)),
        ).fetchone()[0]
        for uid in selected:
            release = by_id[uid]
            window = windows.get(release.key)
            if window is None:
                raise RunError(f"Update {uid} was released at or after the run cutoff.")
            versions = [NoteVersion(*row) for row in conn.execute(
                "SELECT note_version_id, source_updated_at FROM update_note_versions WHERE event_gid = %s",
                (release.key,),
            )]
            note, notes_status = choose_note_version(versions, release.primary.published_at, window.end or cutoff)
            conn.execute(
                """INSERT INTO run_updates (run_id, update_id, window_start, window_end, window_status, window_note,
                                            next_update_id, simultaneous_release, note_version_id, notes_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, uid, window.start, window.end, window.status, window.note,
                 id_by_key.get(window.next_release_key), window.simultaneous,
                 note.note_version_id if note else None, notes_status),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id


def latest_run_status(conn: psycopg.Connection, app_id: int) -> dict[int, tuple[int, str]]:
    """For each update, the most recent run that included it: {update_id: (run_id, status)}."""
    rows = conn.execute(
        """SELECT DISTINCT ON (ru.update_id) ru.update_id, r.run_id, r.status
           FROM run_updates ru JOIN runs r USING (run_id)
           WHERE r.app_id = %s ORDER BY ru.update_id, r.run_id DESC""",
        (app_id,),
    ).fetchall()
    return {update_id: (run_id, status) for update_id, run_id, status in rows}

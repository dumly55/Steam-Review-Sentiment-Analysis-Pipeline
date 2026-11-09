"""Read a run's results from the SQL views, for the control panel and the CLI. No counting happens here."""

import psycopg
from psycopg.rows import dict_row

SOURCE_LABELS = {"review": "Steam reviews", "comment": "Announcement comments"}
SUPPORT_LABELS = {"recurring_praise": "Recurring praise", "recurring_criticism": "Recurring criticism",
                  "recurring_mixed": "Recurring, divided", "recurring_neutral": "Recurring, no clear opinion",
                  "limited": "Limited / isolated"}
TIER_LABELS = {"patch_related": "Patch-related", "tentative": "Tentative (plausibly related)",
               "general": "General game feedback", "unrelated": "Unrelated"}


def _rows(conn: psycopg.Connection, query: str, params: tuple) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def run_summary(conn: psycopg.Connection, run_id: int) -> dict | None:
    rows = _rows(conn, """SELECT r.run_id, r.app_id, g.name AS game, g.is_synthetic, r.status, r.cutoff_at,
                                 r.created_at, r.started_at, r.finished_at, r.error, r.cancel_requested_at,
                                 r.config -> 'analysis' ->> 'model' AS model,
                                 r.config -> 'analysis' -> 'max_items_per_source' AS max_items
                          FROM runs r JOIN games g USING (app_id) WHERE r.run_id = %s""", (run_id,))
    return rows[0] if rows else None


def update_results(conn: psycopg.Connection, run_id: int) -> list[dict]:
    return _rows(conn, "SELECT * FROM v_update_reception WHERE run_id = %s ORDER BY window_start DESC NULLS LAST",
                 (run_id,))


def source_results(conn: psycopg.Connection, run_id: int, update_id: int) -> list[dict]:
    return _rows(conn, """SELECT * FROM v_source_reception WHERE run_id = %s AND update_id = %s
                          ORDER BY source_type DESC""", (run_id, update_id))


def reasons(conn: psycopg.Connection, run_id: int, update_id: int, source_type: str,
            tiers: tuple[str, ...] = ("patch_related",)) -> list[dict]:
    """Feature rows for one source, most-mentioned first, with the patch change text when one is linked."""
    return _rows(conn, """SELECT f.*, c.text AS change_text
                          FROM v_feature_reasons f
                          LEFT JOIN run_note_changes c ON c.run_id = f.run_id AND c.update_id = f.update_id
                                                       AND c.change_id = f.change_id
                          WHERE f.run_id = %s AND f.update_id = %s AND f.source_type = %s AND f.tier = ANY(%s)
                          ORDER BY f.authors DESC, f.observations DESC, f.feature_key""",
                 (run_id, update_id, source_type, list(tiers)))


def feature_name(key: str) -> str:
    return "The update overall" if key == "update_overall" else key.replace("_", " ")


def share_text(count: int, denominator: int, noun: str = "authors") -> str:
    """'24 of 120 analyzed authors (20%)': every percentage is shown with its count and denominator."""
    if not denominator:
        return f"{count} of 0 analyzed {noun}"
    return f"{count} of {denominator} analyzed {noun} ({count / denominator:.0%})"

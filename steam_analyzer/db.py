from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SCHEMA_FILES = [SQL_DIR / "schema.sql", SQL_DIR / "views.sql"]

# Fake app ids used by tests and the synthetic demo start here. Real Steam app ids are far smaller.
SYNTHETIC_APP_IDS_FROM = 999_000_000


def init_db(database_url: str) -> bool:
    """Create the database if it is missing, then apply the schema and views (idempotent).

    Returns True when the database was newly created.
    """
    dbname = conninfo_to_dict(database_url)["dbname"]
    with psycopg.connect(make_conninfo(database_url, dbname="postgres"), autocommit=True) as conn:
        created = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is None
        if created:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    with psycopg.connect(database_url) as conn:
        for path in SCHEMA_FILES:
            conn.execute(path.read_text(encoding="utf-8"))
    return created


def delete_app_data(conn: psycopg.Connection, app_id: int) -> None:
    """Delete everything stored for a synthetic app id. Refuses real app ids."""
    if app_id < SYNTHETIC_APP_IDS_FROM:
        raise ValueError(f"refusing to delete data for app {app_id}: only synthetic app ids can be deleted")
    conn.rollback()
    for statement in [
        "DELETE FROM runs WHERE app_id = %(a)s",  # cascades to run_updates, run_sources, run_feedback
        "DELETE FROM ai_assessments WHERE app_id = %(a)s",  # cascades to feature_observations
        "DELETE FROM feedback_versions WHERE feedback_id IN (SELECT feedback_id FROM feedback_items WHERE app_id = %(a)s)",
        "DELETE FROM feedback_items WHERE app_id = %(a)s",
        "DELETE FROM update_sources WHERE update_id IN (SELECT update_id FROM updates WHERE app_id = %(a)s)",
        "DELETE FROM updates WHERE app_id = %(a)s",
        "DELETE FROM update_note_versions WHERE event_gid IN (SELECT event_gid FROM source_events WHERE app_id = %(a)s)",
        "DELETE FROM source_events WHERE app_id = %(a)s",
        "DELETE FROM catalog_refreshes WHERE app_id = %(a)s",
        "DELETE FROM games WHERE app_id = %(a)s",
    ]:
        conn.execute(statement, {"a": app_id})
    conn.commit()

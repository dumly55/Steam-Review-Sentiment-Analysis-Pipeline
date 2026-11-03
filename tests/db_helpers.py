"""Shared helpers for tests that use the local PostgreSQL from .env with synthetic data."""

import psycopg

from steam_analyzer.config import load_settings

DATABASE_URL = load_settings().database_url


def postgres_reachable() -> bool:
    try:
        psycopg.connect(DATABASE_URL, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


def event(gid, ts, title, event_type=12, body="notes", updatetime=None) -> dict:
    """A synthetic Steam partner event; gids are prefixed so they never collide with real ones."""
    return {"gid": f"test-{gid}", "event_type": event_type, "event_name": title, "rtime32_start_time": ts,
            "hidden": 0, "announcement_body": {"gid": f"test-a{gid}", "posttime": ts, "updatetime": updatetime or ts,
                                               "forum_topic_id": f"test-t{gid}", "body": body}}


def pager(events):
    """Mimic the events feed: newest first; an anchored request returns the anchor again (nothing new)."""
    ordered = sorted(events, key=lambda e: -e["rtime32_start_time"])
    return lambda anchor: ordered if anchor is None else [e for e in ordered if e["gid"] == anchor]


def cleanup_app(conn: psycopg.Connection, app_id: int) -> None:
    conn.rollback()
    for sql in [
        "DELETE FROM runs WHERE app_id = %(a)s",
        "DELETE FROM update_sources WHERE update_id IN (SELECT update_id FROM updates WHERE app_id = %(a)s)",
        "DELETE FROM updates WHERE app_id = %(a)s",
        "DELETE FROM update_note_versions WHERE event_gid IN (SELECT event_gid FROM source_events WHERE app_id = %(a)s)",
        "DELETE FROM source_events WHERE app_id = %(a)s",
        "DELETE FROM catalog_refreshes WHERE app_id = %(a)s",
        "DELETE FROM games WHERE app_id = %(a)s",
    ]:
        conn.execute(sql, {"a": app_id})
    conn.commit()

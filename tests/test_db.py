"""Integration test against the local PostgreSQL in .env; skipped when it is unreachable."""

import psycopg
import pytest

from steam_analyzer.config import load_settings
from steam_analyzer.db import init_db

DATABASE_URL = load_settings().database_url


def _postgres_reachable() -> bool:
    try:
        psycopg.connect(DATABASE_URL, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


@pytest.mark.skipif(not _postgres_reachable(), reason="local PostgreSQL not reachable")
def test_init_db_is_idempotent():
    init_db(DATABASE_URL)
    assert init_db(DATABASE_URL) is False  # second run does not recreate the database
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute("SELECT app_id, name FROM games WHERE app_id = 553850").fetchall()
    assert rows == [(553850, "Helldivers 2")]

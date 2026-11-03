"""Integration test against the local PostgreSQL in .env; skipped when it is unreachable."""

import psycopg
import pytest
from db_helpers import DATABASE_URL, postgres_reachable

from steam_analyzer.db import init_db


@pytest.mark.skipif(not postgres_reachable(), reason="local PostgreSQL not reachable")
def test_init_db_is_idempotent():
    init_db(DATABASE_URL)
    assert init_db(DATABASE_URL) is False  # second run does not recreate the database
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute("SELECT app_id, name FROM games WHERE app_id = 553850").fetchall()
    assert rows == [(553850, "Helldivers 2")]

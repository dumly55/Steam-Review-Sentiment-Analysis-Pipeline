from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


def init_db(database_url: str) -> bool:
    """Create the database if it is missing, then apply sql/schema.sql (idempotent).

    Returns True when the database was newly created.
    """
    dbname = conninfo_to_dict(database_url)["dbname"]
    with psycopg.connect(make_conninfo(database_url, dbname="postgres"), autocommit=True) as conn:
        created = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is None
        if created:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    with psycopg.connect(database_url) as conn:
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    return created

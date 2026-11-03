import argparse
import logging
import sys

from steam_analyzer.config import load_settings


def _print_results(results) -> int:
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.name}: {r.detail}")
    return 0 if all(r.ok for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m steam_analyzer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb", help="create the database if missing and apply sql/schema.sql")
    sub.add_parser("preflight", help="check PostgreSQL, Ollama and GPU")
    sub.add_parser("smoke", help="bounded live check of the Steam reviews, events and comment sources")
    args = parser.parse_args(argv)

    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "initdb":
        from steam_analyzer.db import init_db

        created = init_db(settings.database_url)
        print("Database created and schema applied." if created else "Database exists; schema applied.")
        return 0
    if args.command == "preflight":
        from steam_analyzer.preflight import run_preflight

        return _print_results(run_preflight(settings))
    from steam_analyzer.smoke import run_smoke

    return _print_results(run_smoke(settings.steam_app_id))


if __name__ == "__main__":
    sys.exit(main())

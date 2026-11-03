import argparse
import logging
import sys

from steam_analyzer.config import load_settings


def _print_results(results) -> int:
    for r in results:
        print(f"[{'OK' if r.ok else 'FAIL'}] {r.name}: {r.detail}")
    return 0 if all(r.ok for r in results) else 1


def _print_catalog(settings) -> int:
    import psycopg

    from steam_analyzer.catalog import compute_windows, format_utc as fmt
    from steam_analyzer.catalog_store import live_refresh, load_catalog

    with psycopg.connect(settings.database_url) as conn:
        result = live_refresh(conn, settings.steam_app_id)
        print(f"Refresh {result.refresh_id}: {result.status}, {result.events_seen} events over "
              f"{result.pages_fetched} pages, news API count {result.news_feed_count}"
              + (f", error: {result.error}" if result.error else ""))
        catalog = load_catalog(conn, settings.steam_app_id)
    if catalog.coverage_warning:
        print("WARNING:", catalog.coverage_warning)
    as_of = catalog.verified_through
    windows = compute_windows([e.release for e in catalog.entries], as_of, as_of) if as_of else {}
    print(f"{len(catalog.entries)} releases, {len(catalog.non_releases)} other announcements; windows as of {fmt(as_of)}")
    for e in catalog.entries:
        w = windows.get(e.release.key)
        dupes = f" (+{len(e.release.duplicates)} duplicate post)" if e.release.duplicates else ""
        print(f"  #{e.update_id:<4} {fmt(e.release.released_at)} -> {fmt(w.end if w else None)}  "
              f"[{w.status if w else 'n/a'}]  {e.release.title}{dupes}")
    return 0 if result.status == "complete" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m steam_analyzer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb", help="create the database if missing and apply sql/schema.sql")
    sub.add_parser("catalog", help="refresh the release catalog from Steam and list releases and their windows")
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
    if args.command == "catalog":
        return _print_catalog(settings)
    if args.command == "preflight":
        from steam_analyzer.preflight import run_preflight

        return _print_results(run_preflight(settings))
    from steam_analyzer.smoke import run_smoke

    return _print_results(run_smoke(settings.steam_app_id))


if __name__ == "__main__":
    sys.exit(main())

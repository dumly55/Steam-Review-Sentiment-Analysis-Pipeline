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


def _print_run(conn, run_id: int) -> int:
    from steam_analyzer import results as res
    from steam_analyzer.catalog import format_utc as fmt

    run = res.run_summary(conn, run_id)
    if run is None:
        print(f"Run {run_id} does not exist.")
        return 1
    print(f"Run {run_id} · {run['status']} · cutoff {fmt(run['cutoff_at'])} · model {run['model']}"
          + (f" · capped at {run['max_items']} items per source" if run["max_items"] not in (None, "null") else ""))
    if run["is_synthetic"]:
        print("*** SYNTHETIC DEMO DATA: invented feedback and scripted answers, not real findings ***")
    if run["error"]:
        print("Error:", run["error"])
    for u in res.update_results(conn, run_id):
        print(f"\n== {u['title']}  ({fmt(u['window_start'])} -> {fmt(u['window_end'])}, {u['window_status']})")
        print(f"   Overall: {u['overall_label']}" + (f" ({u['basis']})" if u["basis"] else ""))
        for caveat in u["caveats"]:
            print(f"   - {caveat}")
        for s in res.source_results(conn, run_id, u["update_id"]):
            reason = f" ({s['insufficient_reason']})" if s["insufficient_reason"] else ""
            coverage = f"{s['coverage']:.1%}" if s["coverage"] is not None else "n/a"
            print(f"   {res.SOURCE_LABELS[s['source_type']]}: {s['label']}{reason}")
            print(f"      collection {s['collection_status']} ({s['collection_detail'] or 'no detail'})")
            print(f"      collected {s['collected']}, excluded {s['excluded']} (outside window "
                  f"{s['excluded_outside_window']}, edited after window {s['excluded_edited_after_window']}, "
                  f"non-English {s['excluded_non_english']}, empty {s['excluded_empty_text']}), eligible {s['eligible']}, "
                  f"analyzed {s['analyzed']}, failed {s['failed']}, pending {s['pending']}, cache hits {s['cache_hits']}, "
                  f"coverage {coverage}")
            print(f"      analyzed authors {s['analyzed_authors']} (+{s['unknown_author_items']} items with unknown "
                  f"author); qualifying update authors {s['qualifying_update_authors']}: {s['positive_authors']} "
                  f"positive, {s['negative_authors']} negative, {s['mixed_authors']} mixed")
            if s["with_recommendation"]:
                print(f"      Steam recommendation (separate measure): {s['recommended']} of "
                      f"{s['with_recommendation']} eligible reviews recommend")
            for r in res.reasons(conn, run_id, u["update_id"], s["source_type"])[:8]:
                example = r["negative_example"] or r["positive_example"] or r["any_example"]
                unknown = (f" (+{r['unknown_author_observations']} mentions by unknown authors)"
                           if r["unknown_author_observations"] else "")
                print(f"      * {res.feature_name(r['feature_key'])}: {res.SUPPORT_LABELS[r['support']]}, "
                      f"{res.share_text(r['authors'], r['analyzed_authors'])}{unknown}; +{r['positive_authors']} "
                      f"-{r['negative_authors']} mixed {r['mixed_authors']}; e.g. \"{example}\"")
    return 0 if run["status"] in ("completed", "partial") else 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles and feedback contain emoji
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m steam_analyzer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb", help="create the database if missing and apply sql/schema.sql and sql/views.sql")
    sub.add_parser("catalog", help="refresh the release catalog from Steam and list releases and their windows")
    sub.add_parser("preflight", help="check PostgreSQL, Ollama and GPU")
    sub.add_parser("smoke", help="bounded live check of the Steam reviews, events and comment sources")
    run = sub.add_parser("run", help="create a run for the given update ids and process it in this window")
    run.add_argument("update_ids", type=int, nargs="+", help="update ids, as listed by the catalog command")
    run.add_argument("--max-items", type=int, help="cap items collected per source (the result is labeled partial)")
    process = sub.add_parser("process", help="process or resume an existing run")
    process.add_argument("run_id", type=int)
    show = sub.add_parser("results", help="print a run's results")
    show.add_argument("run_id", type=int)
    sub.add_parser("demo", help="load the SYNTHETIC demo game and run it end to end (no Steam or Ollama needed)")
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
    if args.command == "smoke":
        from steam_analyzer.smoke import run_smoke

        return _print_results(run_smoke(settings.steam_app_id))

    import psycopg

    from steam_analyzer import pipeline

    with psycopg.connect(settings.database_url) as conn:
        if args.command == "run":
            from steam_analyzer.catalog_store import live_refresh
            from steam_analyzer.config import analysis_config
            from steam_analyzer.runs import create_run

            run_id = create_run(conn, settings.steam_app_id, args.update_ids,
                                refresh=lambda: live_refresh(conn, settings.steam_app_id),
                                analysis=analysis_config(settings, args.max_items))
            print(f"Created run {run_id}. Processing; press Ctrl+C to stop, then resume with: "
                  f"python -m steam_analyzer process {run_id}")
            pipeline.process(conn, run_id, settings)
        elif args.command == "process":
            run_id = args.run_id
            pipeline.process(conn, run_id, settings)
        elif args.command == "demo":
            from steam_analyzer import demo
            from steam_analyzer.runs import create_run

            demo.load_demo(conn)
            ids = [r[0] for r in conn.execute("SELECT update_id FROM updates WHERE app_id = %s", (demo.DEMO_APP_ID,))]
            run_id = create_run(conn, demo.DEMO_APP_ID, ids, refresh=lambda: demo.refresh_demo_catalog(conn),
                                analysis=demo.demo_analysis(settings))
            pipeline.process(conn, run_id, settings)
        else:
            run_id = args.run_id
        return _print_run(conn, run_id)


if __name__ == "__main__":
    sys.exit(main())

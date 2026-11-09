"""Local control panel: load the release catalog, select updates, start a run and preview its results.

Changing the selection only updates this page. Nothing is collected or analyzed until Run analysis,
which freezes the run and starts a background worker (`python -m steam_analyzer process <run id>`).
"""

import pandas as pd
import psycopg
import streamlit as st

from steam_analyzer import demo, pipeline, results as res, steam_sources as steam
from steam_analyzer.catalog import RELEASE_TYPE_LABELS, compute_windows, format_utc as fmt
from steam_analyzer.catalog_store import live_refresh, load_catalog
from steam_analyzer.config import analysis_config, load_settings
from steam_analyzer.runs import RunError, create_run, latest_run_status

settings = load_settings()
WINDOW_LABELS = {"closed": "Closed", "ongoing": "Ongoing", "uncertain": "Uncertain"}
TIME_SOURCE_LABELS = {"announcement_published": "Announcement post", "manual_override": "Manual correction",
                      "missing": "Missing"}
SYNTHETIC_BANNER = ("**SYNTHETIC DEMO DATA.** An invented game with invented reviews and comments, analyzed by a "
                    "script instead of a model. Nothing shown here is a real finding.")

st.set_page_config(page_title="Steam Update Reception", layout="wide")
state = st.session_state
state.setdefault("selected", set())
state.setdefault("editor_version", 0)

demo_mode = st.sidebar.toggle("Synthetic demo data", key="demo_mode",
                              help="Try the whole pipeline offline on an invented game. No Steam or Ollama needed.")
APP_ID = demo.DEMO_APP_ID if demo_mode else settings.steam_app_id


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def reset_editor(selected: set[int]) -> None:
    state.selected = selected
    state.editor_version += 1  # a new editor key makes the table redraw from `selected`


st.title("Steam Update Reception Analyzer")
st.caption(f"{'Synthetic demo game' if demo_mode else f'Steam app {APP_ID}'} · all times are UTC")
if demo_mode:
    st.warning(SYNTHETIC_BANNER)

refresh_label = "Load / reset demo data" if demo_mode else "Load / refresh updates"
if st.button(refresh_label, key="refresh"):
    with st.spinner("Loading the synthetic demo..." if demo_mode else "Reading the full announcement history from Steam..."), \
            connect() as conn:
        if demo_mode:
            demo.load_demo(conn)
        else:
            result = live_refresh(conn, APP_ID)
            if result.status != "complete":
                st.error(f"Catalog refresh failed ({result.error}). Showing the last complete catalog instead.")

try:
    with connect() as conn:
        catalog = load_catalog(conn, APP_ID)
        run_status = latest_run_status(conn, APP_ID)
        busy = pipeline.worker_active(conn, APP_ID)
except psycopg.Error as exc:
    st.error(f"Cannot reach PostgreSQL: {exc}\n\nRun `python -m steam_analyzer preflight` for details.")
    st.stop()

if catalog.refresh_id is None:
    st.info(f"No release catalog yet. Click **{refresh_label}**.")
    st.stop()

st.markdown(
    f"Catalog last verified **{fmt(catalog.verified_through)}** · **{len(catalog.entries)}** releases "
    f"from {catalog.events_seen} announcements"
)
if catalog.coverage_warning:
    st.warning(catalog.coverage_warning)

# Windows as of the last verified refresh; a run recomputes and freezes them at its own cutoff.
as_of = catalog.verified_through
windows = compute_windows([e.release for e in catalog.entries], as_of, as_of)

rows = []
for entry in reversed(catalog.entries):  # newest first
    release, window = entry.release, windows.get(entry.release.key)
    last_run = run_status.get(entry.update_id)
    rows.append({
        "update_id": entry.update_id,
        "Select": entry.update_id in state.selected,
        "Update": release.title,
        "Version": release.version or "",
        "Type": RELEASE_TYPE_LABELS.get(release.release_type, release.release_type),
        "Released": fmt(release.released_at),
        "Time source": TIME_SOURCE_LABELS[release.released_at_source],
        "Window ends": ("ongoing" if window.status == "ongoing" else fmt(window.end)) if window else "—",
        "Window": WINDOW_LABELS[window.status] if window else "Uncertain",
        "Posts": 1 + len(release.duplicates),
        "Announcement": steam.ANNOUNCEMENT_URL.format(app_id=APP_ID, event_gid=release.key),
        "Comments": (steam.COMMENTS_URL.format(app_id=APP_ID, topic_id=release.primary.forum_topic_id)
                     if release.primary.forum_topic_id else None),
        "Analysis": f"Run #{last_run[0]}: {last_run[1]}" if last_run else "Not analyzed",
    })
table = pd.DataFrame(rows).set_index("update_id")
all_ids = set(table.index)

left, middle, _ = st.columns([1, 1, 5])
if left.button("Select all", key="select_all"):
    reset_editor(set(all_ids))
    st.rerun()
if middle.button("Clear selection", key="clear"):
    reset_editor(set())
    st.rerun()

edited = st.data_editor(
    table,
    key=f"catalog_editor_{state.editor_version}",
    hide_index=True,
    width="stretch",
    height=420,
    disabled=[c for c in table.columns if c != "Select"],
    column_config={
        "Select": st.column_config.CheckboxColumn("Select", width="small"),
        "Posts": st.column_config.NumberColumn("Posts", help="Announcement posts describing this release"),
        "Announcement": st.column_config.LinkColumn("Announcement", display_text="Open"),
        "Comments": st.column_config.LinkColumn("Comments", display_text="Open"),
        "Window ends": st.column_config.TextColumn(
            "Window ends", help="Feedback counts for an update from its release until the next release."),
    },
)
state.selected = set(edited.index[edited["Select"]]) & all_ids

count = len(state.selected)
scope = " (every update in the loaded catalog)" if count == len(all_ids) else ""
st.markdown(f"**{count} of {len(all_ids)}** updates selected{scope}")
if busy:
    st.info("A run is being processed in the background. Start another one when it finishes.")

if st.button("Run analysis", key="run", type="primary", disabled=count == 0 or busy):
    with st.spinner("Refreshing the catalog and freezing feedback windows..."), connect() as conn:
        if demo_mode:
            refresh, analysis = (lambda: demo.refresh_demo_catalog(conn)), demo.demo_analysis(settings)
        else:
            refresh, analysis = (lambda: live_refresh(conn, APP_ID)), analysis_config(settings)
        try:
            run_id = create_run(conn, APP_ID, sorted(state.selected), refresh=refresh, analysis=analysis)
        except RunError as exc:
            st.error(str(exc))
        else:
            pipeline.launch_worker(run_id)
            state.run_picker = run_id
            st.rerun()

with st.expander(f"Announcements not treated as releases ({len(catalog.non_releases)})"):
    st.caption("News, sales, events and advance patch announcements never start or end a feedback window.")
    st.dataframe(
        pd.DataFrame([{"Published": fmt(e.published_at), "Title": e.title, "Steam event type": e.event_type}
                      for e in reversed(catalog.non_releases)]),
        hide_index=True, width="stretch",
    )


# --- Run results -----------------------------------------------------------------------------------

def source_table(sources: list[dict]) -> pd.DataFrame:
    """Metrics down the side, one column per source. Counts always sit next to their denominators."""
    def pct(x):
        return f"{x:.1%}" if x is not None else "—"

    columns = {}
    for s in sources:
        columns[res.SOURCE_LABELS[s["source_type"]]] = {
            "Label": s["label"] + (f" ({s['insufficient_reason']})" if s["insufficient_reason"] else ""),
            "Collection": f"{s['collection_status']}: {s['collection_detail'] or '—'}",
            "Collected items": s["collected"],
            "Excluded: posted outside the window": s["excluded_outside_window"],
            "Excluded: edited after the window": s["excluded_edited_after_window"],
            "Excluded: not English": s["excluded_non_english"],
            "Excluded: empty text": s["excluded_empty_text"],
            "Eligible items": s["eligible"],
            "Analyzed / failed / pending": f"{s['analyzed']} / {s['failed']} / {s['pending']}",
            "Analysis coverage": pct(s["coverage"]),
            "Cache hits": s["cache_hits"],
            "Analyzed authors (identifiable)": s["analyzed_authors"],
            "Analyzed items with unknown author": s["unknown_author_items"],
            "Qualifying update authors": s["qualifying_update_authors"],
            "  positive / negative / mixed": f"{s['positive_authors']} / {s['negative_authors']} / {s['mixed_authors']}",
            "Text sentiment +/−/mixed/neutral (items)":
                f"{s['text_positive']} / {s['text_negative']} / {s['text_mixed']} / {s['text_neutral_unclear']}",
            "Steam recommendation (separate measure)":
                f"{s['recommended']} of {s['with_recommendation']} recommend ({pct(s['recommendation_share'])})"
                if s["with_recommendation"] else "—",
        }
    return pd.DataFrame(columns).astype(str)


def reasons_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Feature": res.feature_name(r["feature_key"]),
        "Patch change": f"{r['change_id']}: {r['change_text']}" if r["change_id"] else "",
        "Category": r["category"],
        "Support": res.SUPPORT_LABELS[r["support"]],
        "Authors": res.share_text(r["authors"], r["analyzed_authors"]),
        "Positive": r["positive_authors"],
        "Negative": r["negative_authors"],
        "Mixed": r["mixed_authors"],
        "Neutral / unclear": r["neutral_unclear_authors"],
        "Unknown-author mentions": r["unknown_author_observations"],
        "Example": r["negative_example"] or r["positive_example"] or r["any_example"],
        "Source": r["negative_example_link"] or r["positive_example_link"] or r["any_example_link"],
    } for r in rows])


REASON_COLUMNS = {"Source": st.column_config.LinkColumn("Source", display_text="Open")}


def progress_table(updates: list[dict], detail: dict) -> pd.DataFrame:
    """One row per update and source: how far collection and analysis have got."""
    rows = []
    for u in updates:
        sources = detail[u["update_id"]][0]
        if not sources:
            rows.append({"Update": u["title"], "Collection": "not started"})
        for s in sources:
            rows.append({
                "Update": u["title"],
                "Source": res.SOURCE_LABELS[s["source_type"]],
                "Collection": s["collection_status"],
                "Collected": s["collected"],
                "Excluded": s["excluded"],
                "Eligible": s["eligible"],
                "Cache hits": s["cache_hits"],
                "Analyzed": s["analyzed"],
                "Failed": s["failed"],
                "Pending": s["pending"],
                "Done": (s["analyzed"] + s["failed"]) / s["eligible"] if s["eligible"] else None,
            })
    return pd.DataFrame(rows)


def show_run(run_id: int, was_active: bool = False) -> None:
    with connect() as conn:
        run = res.run_summary(conn, run_id)
        active = pipeline.worker_active(conn, run["app_id"])
        updates = res.update_results(conn, run_id)
        detail = {u["update_id"]: (res.source_results(conn, run_id, u["update_id"]),
                                   {s: (res.reasons(conn, run_id, u["update_id"], s),
                                        res.reasons(conn, run_id, u["update_id"], s,
                                                    ("tentative", "general", "unrelated")))
                                    for s in ("review", "comment")})
                  for u in updates}
    if was_active and not active:
        st.rerun()  # the worker just finished: redraw the whole page, not only this panel

    st.subheader(f"Run #{run_id} · {run['status']}")
    st.caption(f"Cutoff {fmt(run['cutoff_at'])} · model {run['model']} · created {fmt(run['created_at'])}"
               + (f" · finished {fmt(run['finished_at'])}" if run["finished_at"] else ""))
    if run["is_synthetic"]:
        st.warning(SYNTHETIC_BANNER)
    if run["max_items"] is not None:
        st.warning(f"Capped at {run['max_items']} items per source: collection is incomplete, so no source "
                   "can receive a reception label.")
    unfinished = run["status"] in ("queued", *pipeline.ACTIVE_STATUSES, "failed", "cancelled")
    if run["status"] == "failed":
        st.error(f"The run stopped: {run['error']}")
    if unfinished and active:
        if run["cancel_requested_at"]:
            st.info("Cancelling: the run stops after the items it is working on.")
        else:
            st.info(f"Processing in the background (log: data/logs/run_{run_id}.log). This panel updates "
                    "every few seconds.")
            if st.button("Cancel run", key=f"cancel_{run_id}"):
                with connect() as conn:
                    pipeline.request_cancel(conn, run_id)
                st.rerun()
    elif unfinished:
        st.warning("This run is not being processed right now. Finished work is kept; resuming continues from there.")
        if st.button("Resume run", key=f"resume_{run_id}"):
            pipeline.launch_worker(run_id)
            st.rerun()
    if unfinished:
        st.dataframe(progress_table(updates, detail), hide_index=True, width="stretch", column_config={
            "Done": st.column_config.ProgressColumn("Done", help="Analyzed or failed, out of eligible items",
                                                    min_value=0.0, max_value=1.0, format="percent")})

    for u in updates:
        sources, by_source = detail[u["update_id"]]
        with st.container(border=True):
            st.markdown(f"#### {u['title']}")
            end = "ongoing" if u["window_status"] == "ongoing" else fmt(u["window_end"])
            st.caption(f"Feedback window {fmt(u['window_start'])} → {end} · results as of {fmt(u['as_of'])}")
            st.markdown(f"**Overall reception among analyzed authors: {u['overall_label']}**"
                        + (f" ({u['basis']})" if u["basis"] else ""))
            for caveat in u["caveats"]:
                st.caption(f"⚠️ {caveat}")
            if sources:
                st.dataframe(source_table(sources), width="stretch")
            tabs = st.tabs([res.SOURCE_LABELS[s] for s in ("review", "comment")])
            for tab, source_type in zip(tabs, ("review", "comment")):
                principal, other = by_source[source_type]
                with tab:
                    st.caption("Patch-related reasons: feedback tied to this update or one of its listed changes. "
                               "Each author counts once per feature; shares use analyzed authors as the denominator.")
                    if principal:
                        st.dataframe(reasons_table(principal), hide_index=True, width="stretch",
                                     column_config=REASON_COLUMNS)
                    else:
                        st.caption("No patch-related observations.")
                    with st.expander(f"Tentative and general topics ({len(other)})"):
                        if other:
                            frame = reasons_table(other)
                            frame.insert(1, "Tier", [res.TIER_LABELS[r["tier"]] for r in other])
                            st.dataframe(frame, hide_index=True, width="stretch", column_config=REASON_COLUMNS)


live_run = st.fragment(run_every=3)(show_run)  # re-reads progress every 3 s while a worker is busy

st.divider()
st.header("Runs")
with connect() as conn:
    recent = conn.execute("SELECT run_id, status, created_at FROM runs WHERE app_id = %s ORDER BY run_id DESC LIMIT 25",
                          (APP_ID,)).fetchall()
if not recent:
    st.caption("No runs yet. Select updates and click Run analysis.")
else:
    ids = [r[0] for r in recent]
    labels = {run_id: f"Run #{run_id} · {status} · created {fmt(created)}" for run_id, status, created in recent}
    if state.get("run_picker") not in ids:  # e.g. after switching between demo and real data
        state.pop("run_picker", None)
    picked = st.selectbox("Show run", ids, format_func=labels.get, key="run_picker")
    (live_run if busy else show_run)(picked, busy)

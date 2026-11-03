"""Local control panel: load the release catalog, select updates and start a run.

Changing the selection only updates this page. Nothing is collected or analyzed until Run analysis.
"""

import pandas as pd
import psycopg
import streamlit as st

from steam_analyzer import steam_sources as steam
from steam_analyzer.catalog import RELEASE_TYPE_LABELS, compute_windows, format_utc as fmt
from steam_analyzer.catalog_store import live_refresh, load_catalog
from steam_analyzer.config import load_settings
from steam_analyzer.runs import RunError, create_run, latest_run_status

settings = load_settings()
APP_ID = settings.steam_app_id
WINDOW_LABELS = {"closed": "Closed", "ongoing": "Ongoing", "uncertain": "Uncertain"}
TIME_SOURCE_LABELS = {"announcement_published": "Announcement post", "manual_override": "Manual correction",
                      "missing": "Missing"}

st.set_page_config(page_title="Steam Update Reception", layout="wide")
state = st.session_state
state.setdefault("selected", set())
state.setdefault("editor_version", 0)


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def reset_editor(selected: set[int]) -> None:
    state.selected = selected
    state.editor_version += 1  # a new editor key makes the table redraw from `selected`


st.title("Steam Update Reception Analyzer")
st.caption(f"Steam app {APP_ID} · all times are UTC")

if st.button("Load / refresh updates", key="refresh"):
    with st.spinner("Reading the full announcement history from Steam..."), connect() as conn:
        result = live_refresh(conn, APP_ID)
    if result.status != "complete":
        st.error(f"Catalog refresh failed ({result.error}). Showing the last complete catalog instead.")

try:
    with connect() as conn:
        catalog = load_catalog(conn, APP_ID)
        run_status = latest_run_status(conn, APP_ID)
except psycopg.Error as exc:
    st.error(f"Cannot reach PostgreSQL: {exc}\n\nRun `python -m steam_analyzer preflight` for details.")
    st.stop()

if catalog.refresh_id is None:
    st.info("No release catalog yet. Click **Load / refresh updates**.")
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

if st.button("Run analysis", key="run", type="primary", disabled=count == 0):
    with st.spinner("Refreshing the catalog and freezing feedback windows..."), connect() as conn:
        try:
            state.last_run = create_run(conn, APP_ID, sorted(state.selected), refresh=lambda: live_refresh(conn, APP_ID))
        except RunError as exc:
            st.error(str(exc))
        else:
            st.rerun()

if "last_run" in state:
    with connect() as conn:
        cutoff, status = conn.execute(
            "SELECT cutoff_at, status FROM runs WHERE run_id = %s", (state.last_run,)).fetchone()
        frozen = pd.DataFrame(
            [(title, fmt(start), fmt(end), WINDOW_LABELS[w], nxt or "—", notes.replace("_", " "), note)
             for title, start, end, w, nxt, notes, note in conn.execute(
                """SELECT u.title, ru.window_start, ru.window_end, ru.window_status, n.title, ru.notes_status,
                          ru.window_note
                   FROM run_updates ru JOIN updates u USING (update_id)
                   LEFT JOIN updates n ON n.update_id = ru.next_update_id
                   WHERE ru.run_id = %s ORDER BY ru.window_start DESC""", (state.last_run,))],
            columns=["Update", "Window start", "Window end", "Window", "Ends at release", "Patch notes", "Note"])
    st.subheader(f"Run #{state.last_run} · {status}")
    st.caption(f"Cutoff {fmt(cutoff)}. Windows and patch notes are frozen for this run.")
    st.dataframe(frozen, hide_index=True, width="stretch")
    st.info("Feedback collection and analysis are not built yet, so this run stays queued.")

with st.expander(f"Announcements not treated as releases ({len(catalog.non_releases)})"):
    st.caption("News, sales, events and advance patch announcements never start or end a feedback window.")
    st.dataframe(
        pd.DataFrame([{"Published": fmt(e.published_at), "Title": e.title, "Steam event type": e.event_type}
                      for e in reversed(catalog.non_releases)]),
        hide_index=True, width="stretch",
    )

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from steam_analyzer.catalog import (
    AnnouncementEvent, NoteVersion, Override, choose_note_version, classify, compute_windows, format_utc,
)

FIXTURE = Path(__file__).parent / "fixtures" / "helldivers2_events_2026-11-02.json"
REAL_EVENTS = [AnnouncementEvent.from_steam(e) for e in json.loads(FIXTURE.read_text(encoding="utf-8"))["events"]]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SEC = timedelta(seconds=1)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def ev(gid: str, hours: float | None, title: str = "Patch 1.0.0", event_type: int = 12) -> AnnouncementEvent:
    t = at(hours) if hours is not None else None
    return AnnouncementEvent(gid, event_type, title, t, t, t, f"a{gid}", f"t{gid}")


def by_title(releases, title):
    return next(r for r in releases if r.title == title)


# --- AC01 on the real Helldivers 2 history --------------------------------------------------------

def test_real_history_splits_into_releases_and_other_announcements():
    releases, others = classify(REAL_EVENTS)
    assert (len(releases), len(others)) == (95, 72)
    assert all(r.primary.event_type in (12, 13, 14) for r in releases)
    assert all(e.event_type not in (12, 13, 14) for e in others)


def test_news_and_advance_announcements_never_become_releases():
    releases, others = classify(REAL_EVENTS)
    release_titles = {r.title for r in releases} | {d.title for r in releases for d in r.duplicates}
    for title in ["PATCH 1.000.400 explained", "Optimizing Liberty - Incoming Patch: 27th May",
                  "Patch Delay Update", "Scheduled Maintenance on May 19", "Escalation of Freedom Livestream Event"]:
        assert title not in release_titles
        assert any(e.title == title for e in others)


def test_second_post_about_the_same_release_is_merged_and_kept_as_a_source():
    releases, _ = classify(REAL_EVENTS)
    release = by_title(releases, "Devoid of Liberty: 7.0.0")
    assert [d.title for d in release.duplicates] == ["HELLDIVERS 2 Devoid of Liberty Update Out Now"]
    assert release.released_at == release.primary.published_at  # the earlier of the two posts


def test_release_without_a_version_number_is_kept():
    releases, _ = classify(REAL_EVENTS)
    release = by_title(releases, "Into the Unjust - Out Now!")
    assert release.version is None and release.duplicates == ()


def test_version_number_reused_months_apart_stays_two_releases():
    releases, _ = classify(REAL_EVENTS)
    assert len([r for r in releases if r.version == "01.002.203"]) == 2


def test_real_patch_notes_edited_after_the_next_release_are_flagged():
    releases, _ = classify(REAL_EVENTS)
    release = by_title(releases, "Into the Unjust: 4.1.2")
    end = compute_windows(releases, at(1e5), at(1e5))[release.key].end
    assert release.primary.body_updated_at >= end  # Steam shows the notes were edited after 5.0.0 shipped
    version = NoteVersion(1, release.primary.body_updated_at)
    assert choose_note_version([version], release.primary.published_at, end) == (version, "edited_after_window")


# --- Release grouping --------------------------------------------------------------------------------

def test_hotfix_gets_its_own_entry_and_boundary():
    releases, _ = classify([ev("1", 0, "Patch 2.0.0", 14), ev("2", 30, "Hotfix 2.0.1"), ev("3", 200, "Patch 2.1.0", 13)])
    windows = compute_windows(releases, at(1000), at(1000))
    assert len(releases) == 3
    assert windows["1"].end == at(30)


def test_same_day_hotfix_with_a_new_version_is_not_merged():
    releases, _ = classify([ev("1", 0, "Patch 2.0.0"), ev("2", 3, "Hotfix 2.0.1")])
    assert len(releases) == 2


def test_sale_announcement_between_updates_does_not_end_a_window():
    releases, _ = classify([ev("1", 0, "Patch 2.0.0"), ev("sale", 50, "Weekend sale!", 28), ev("2", 100, "Patch 2.0.1")])
    assert compute_windows(releases, at(1000), at(1000))["1"].end == at(100)


# --- Windows (AC03, AC04, AC05, AC19) ----------------------------------------------------------------

THREE = [ev("1", 0, "Update 1.0.0"), ev("2", 100, "Update 2.0.0"), ev("3", 150, "Update 3.0.0")]


def test_window_ends_at_the_next_release_whatever_is_selected():
    # Windows are computed from the whole catalog; there is no selection input to change them.
    releases, _ = classify(THREE)
    window = compute_windows(releases, at(1000), at(1000))["2"]
    assert (window.start, window.end, window.status, window.next_release_key) == (at(100), at(150), "closed", "3")
    assert window.contains(at(150) - SEC)


def test_feedback_exactly_at_a_release_belongs_only_to_the_newer_update():
    releases, _ = classify(THREE)
    windows = compute_windows(releases, at(1000), at(1000))
    assert not windows["2"].contains(at(150))
    assert windows["3"].contains(at(150))


def test_latest_update_ends_at_the_cutoff_and_excludes_older_feedback():
    releases, _ = classify(THREE)
    cutoff = at(150 + 240)
    window = compute_windows(releases, cutoff, catalog_verified_through=cutoff)["3"]
    assert (window.end, window.status) == (cutoff, "ongoing")
    assert not window.contains(at(150) - SEC)
    assert window.contains(cutoff - SEC) and not window.contains(cutoff)


def test_releases_after_the_cutoff_are_outside_the_run():
    releases, _ = classify(THREE + [ev("4", 500, "Update 4.0.0")])
    windows = compute_windows(releases, at(400), at(400))
    assert "4" not in windows
    assert windows["3"].end == at(400)


def test_catalog_not_checked_through_the_cutoff_makes_only_the_latest_uncertain():
    releases, _ = classify(THREE)
    windows = compute_windows(releases, at(400), catalog_verified_through=at(300))
    assert windows["3"].status == "uncertain" and windows["3"].end == at(400)
    assert windows["2"].status == "closed"
    assert compute_windows(releases, at(400), None)["3"].status == "uncertain"


def test_release_without_a_timestamp_makes_every_boundary_uncertain():
    releases, _ = classify(THREE + [ev("x", None, "Update 9.9.9")])
    windows = compute_windows(releases, at(400), at(400))
    assert windows["x"].start is None and windows["x"].end is None
    assert {w.status for w in windows.values()} == {"uncertain"}


def test_simultaneous_releases_are_flagged_and_share_the_next_boundary():
    releases, _ = classify([ev("a", 0, "Patch 3.0.0"), ev("b", 0, "Patch 3.0.1"), ev("c", 48, "Patch 3.1.0")])
    windows = compute_windows(releases, at(100), at(100))
    for key in ("a", "b"):
        assert windows[key].simultaneous and windows[key].end == at(48)
    assert not windows["c"].simultaneous


# --- Manual overrides ----------------------------------------------------------------------------------

def test_release_time_override_records_its_provenance():
    releases, _ = classify([ev("1", 0)], {"1": Override("1", released_at=at(2), reason="patch went live late")})
    assert (releases[0].released_at, releases[0].released_at_source) == (at(2), "manual_override")


def test_override_can_include_or_exclude_an_event():
    events = [ev("news", 0, "Surprise patch 5.0.1", 28), ev("teaser", 10, "Patch 5.0.2 teaser", 12)]
    overrides = {"news": Override("news", is_release=True), "teaser": Override("teaser", is_release=False)}
    releases, others = classify(events, overrides)
    assert [r.key for r in releases] == ["news"]
    assert [e.event_gid for e in others] == ["teaser"]


def test_times_are_always_shown_in_utc():
    pacific = timezone(timedelta(hours=-7))
    assert format_utc(datetime(2026, 9, 24, 2, 0, tzinfo=pacific)) == "2026-09-24 09:00 UTC"
    assert format_utc(None) == "—"


# --- Patch-note snapshots --------------------------------------------------------------------------------

def test_note_version_choice():
    end = at(100)
    original, edited_late = NoteVersion(1, at(0)), NoteVersion(2, at(200))
    assert choose_note_version([original], at(0), end) == (original, "unedited")
    assert choose_note_version([NoteVersion(3, at(5))], at(0), end)[1] == "edited_during_window"
    assert choose_note_version([edited_late], at(0), end) == (edited_late, "edited_after_window")
    assert choose_note_version([original, edited_late], at(0), end) == (original, "unedited")  # archived copy wins
    assert choose_note_version([], at(0), end) == (None, "missing")

"""Release catalog rules: which announcement events are real releases, and each release's feedback window.

Pure functions only (no database, no network). Persistence lives in catalog_store.py.

Window rule:
    start = the release's timestamp
    end   = the next strictly later release, or the run cutoff for the latest release
    feedback is eligible when start <= created_at < end
"""

import json
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from steam_analyzer.steam_sources import RELEASE_EVENT_TYPES

VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
RELEASE_TYPE_LABELS = {"small_update": "Small update", "regular_update": "Regular update", "major_update": "Major update"}

# Release-time provenance values stored with each update.
FROM_ANNOUNCEMENT = "announcement_published"
FROM_OVERRIDE = "manual_override"
MISSING = "missing"


def _utc(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def format_utc(t: datetime | None) -> str:
    """Display form for any aware datetime. PostgreSQL returns the session's local zone, so convert first."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "—"


@dataclass(frozen=True)
class AnnouncementEvent:
    event_gid: str
    event_type: int
    title: str
    published_at: datetime | None
    event_start_at: datetime | None
    body_updated_at: datetime | None
    announcement_gid: str | None
    forum_topic_id: str | None
    hidden: bool = False

    @classmethod
    def from_steam(cls, event: dict) -> "AnnouncementEvent":
        body = event.get("announcement_body") or {}
        return cls(
            event_gid=str(event["gid"]),
            event_type=int(event["event_type"]),
            title=(event.get("event_name") or body.get("headline") or "").strip(),
            published_at=_utc(body.get("posttime")),
            event_start_at=_utc(event.get("rtime32_start_time")),
            body_updated_at=_utc(body.get("updatetime")),
            announcement_gid=str(body["gid"]) if body.get("gid") else None,
            forum_topic_id=str(body["forum_topic_id"]) if body.get("forum_topic_id") else None,
            hidden=bool(event.get("hidden")),
        )


@dataclass(frozen=True)
class Override:
    """A manual correction from config/release_overrides.json, keyed by event gid."""

    event_gid: str
    is_release: bool | None = None
    released_at: datetime | None = None
    reason: str = ""


def load_overrides(path: Path) -> dict[str, Override]:
    if not path.exists():
        return {}
    overrides = {}
    for item in json.loads(path.read_text(encoding="utf-8")):
        released_at = item.get("released_at")
        overrides[str(item["event_gid"])] = Override(
            event_gid=str(item["event_gid"]),
            is_release=item.get("is_release"),
            released_at=datetime.fromisoformat(released_at).astimezone(timezone.utc) if released_at else None,
            reason=item.get("reason", ""),
        )
    return overrides


@dataclass(frozen=True)
class Release:
    primary: AnnouncementEvent
    duplicates: tuple[AnnouncementEvent, ...]
    version: str | None
    released_at: datetime | None
    released_at_source: str

    @property
    def key(self) -> str:
        return self.primary.event_gid

    @property
    def title(self) -> str:
        return self.primary.title

    @property
    def release_type(self) -> str:
        return RELEASE_EVENT_TYPES.get(self.primary.event_type, "manual")


def version_of(title: str) -> str | None:
    match = VERSION_RE.search(title)
    return match.group(0) if match else None


def classify(
    events: list[AnnouncementEvent],
    overrides: dict[str, Override] | None = None,
    duplicate_merge_hours: float = 12,
) -> tuple[list[Release], list[AnnouncementEvent]]:
    """Split events into releases (oldest first) and non-release announcements.

    A release is a Steam update-type event (small/regular/major), unless an override says otherwise.
    Posts describing the same release are merged: within `duplicate_merge_hours` of each other, an event
    without a version number, or with the same version, joins the earlier group. Version numbers alone are
    not trusted because real titles reuse or mistype them months apart.
    """
    overrides = overrides or {}

    def is_release(e: AnnouncementEvent) -> bool:
        override = overrides.get(e.event_gid)
        if override and override.is_release is not None:
            return override.is_release
        return e.event_type in RELEASE_EVENT_TYPES and not e.hidden

    def sort_key(e: AnnouncementEvent):
        return (e.published_at is None, e.published_at or datetime.min.replace(tzinfo=timezone.utc), e.event_gid)

    candidates = sorted((e for e in events if is_release(e)), key=sort_key)
    non_releases = sorted((e for e in events if not is_release(e)), key=sort_key)

    window = timedelta(hours=duplicate_merge_hours)
    groups: list[list[AnnouncementEvent]] = []
    for event in candidates:
        target = None
        if event.published_at is not None:
            version = version_of(event.title)
            for group in reversed(groups):
                if group[0].published_at is None or event.published_at - group[0].published_at > window:
                    break
                group_version = next((v for v in map(lambda e: version_of(e.title), group) if v), None)
                if version is None or group_version is None or version == group_version:
                    target = group
                    break
        if target is None:
            groups.append([event])
        else:
            target.append(event)

    releases = []
    for group in groups:
        primary = next((e for e in group if version_of(e.title)), group[0])
        override_time = next((overrides[e.event_gid].released_at for e in group
                              if e.event_gid in overrides and overrides[e.event_gid].released_at), None)
        published = [e.published_at for e in group if e.published_at]
        if override_time:
            released_at, source = override_time, FROM_OVERRIDE
        elif published:
            released_at, source = min(published), FROM_ANNOUNCEMENT
        else:
            released_at, source = None, MISSING
        releases.append(Release(
            primary=primary,
            duplicates=tuple(e for e in group if e is not primary),
            version=version_of(primary.title),
            released_at=released_at,
            released_at_source=source,
        ))
    releases.sort(key=lambda r: (r.released_at is None, r.released_at or datetime.min.replace(tzinfo=timezone.utc), r.key))
    return releases, non_releases


@dataclass(frozen=True)
class Window:
    start: datetime | None
    end: datetime | None
    status: str  # closed | ongoing | uncertain
    next_release_key: str | None
    simultaneous: bool
    note: str = ""

    def contains(self, created_at: datetime) -> bool:
        return self.start is not None and self.end is not None and self.start <= created_at < self.end


def compute_windows(
    releases: list[Release], cutoff: datetime, catalog_verified_through: datetime | None
) -> dict[str, Window]:
    """Feedback window for every release, computed from the whole catalog regardless of what is selected.

    `catalog_verified_through` is when the last complete catalog refresh started. The latest release is
    only "ongoing" if the catalog was checked at or after the cutoff; otherwise a newer release might exist.
    Releases at or after the cutoff are outside this run and get no window.
    """
    undated = [r for r in releases if r.released_at is None]
    dated = sorted((r for r in releases if r.released_at is not None and r.released_at < cutoff),
                   key=lambda r: (r.released_at, r.key))
    times = sorted({r.released_at for r in dated})
    first_at_time: dict[datetime, str] = {}
    for r in dated:
        first_at_time.setdefault(r.released_at, r.key)
    same_time = Counter(r.released_at for r in dated)

    windows = {
        r.key: Window(None, None, "uncertain", None, False, "Release time is missing; add a manual override.")
        for r in undated
    }
    for r in dated:
        notes = []
        later = bisect_right(times, r.released_at)
        if later < len(times):
            end, next_key, status = times[later], first_at_time[times[later]], "closed"
        else:
            end, next_key = cutoff, None
            if catalog_verified_through is not None and catalog_verified_through >= cutoff:
                status = "ongoing"
            else:
                status = "uncertain"
                notes.append("The catalog was not checked through the cutoff, so a newer release may exist.")
        if undated:
            status = "uncertain"
            notes.append("A release in the catalog has no timestamp, so this boundary cannot be verified.")
        simultaneous = same_time[r.released_at] > 1
        if simultaneous:
            notes.append("Released at the same time as another update; feedback cannot be attributed to only one.")
        windows[r.key] = Window(r.released_at, end, status, next_key, simultaneous, " ".join(notes))
    return windows


@dataclass(frozen=True)
class NoteVersion:
    note_version_id: int
    source_updated_at: datetime | None


def choose_note_version(
    versions: list[NoteVersion], published_at: datetime | None, window_end: datetime
) -> tuple[NoteVersion | None, str]:
    """Pick the patch-note snapshot that was current during the window.

    Returns the version and a status:
      unedited              notes never edited after publication
      edited_during_window  last edit happened before the window ended
      edited_after_window   only a version edited after the window ended was captured; context is provisional
      missing               no notes captured
    """
    if not versions:
        return None, "missing"
    in_window = [v for v in versions if v.source_updated_at is not None and v.source_updated_at < window_end]
    if in_window:
        chosen = max(in_window, key=lambda v: v.source_updated_at)
        return chosen, "unedited" if chosen.source_updated_at == published_at else "edited_during_window"
    latest = max(versions, key=lambda v: (v.source_updated_at is not None, v.source_updated_at))
    return latest, "edited_after_window"

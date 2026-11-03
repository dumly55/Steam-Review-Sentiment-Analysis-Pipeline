"""Feedback records and the deterministic rules for which feedback counts in an update's window.

Pure functions only (no database, no network). Every check here runs before the model sees anything,
so an excluded item never reaches inference or the coverage denominator.

Eligibility, in order:
    outside_window       created_at is not in [window start, window end)
    edited_after_window  every captured version was last edited at or after the window end
    non_english          outside the English-only language scope
    empty_text           nothing to analyze
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from steam_analyzer.steam_sources import COMMENTS_URL, Comment

REVIEW_URL = "https://steamcommunity.com/profiles/{steamid}/recommended/{app_id}/"

# Language values that are in scope. Reviews carry Steam's own tag; comments get detect_language().
IN_SCOPE_LANGUAGES = {"english", "uncertain"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass(frozen=True)
class FeedbackRecord:
    """One review or comment as Steam showed it at collection time."""

    source_type: str          # review | comment
    source_item_id: str
    author_id: str | None     # steamid64
    created_at: datetime
    edited_at: datetime       # last edit Steam showed; created_at when never edited
    text: str
    voted_up: bool | None
    language: str
    permalink: str
    raw: dict
    thread_id: str | None = None
    event_gid: str | None = None


def review_record(raw: dict, app_id: int) -> FeedbackRecord:
    steamid = (raw.get("author") or {}).get("steamid")
    created = int(raw["timestamp_created"])
    return FeedbackRecord(
        source_type="review",
        source_item_id=str(raw["recommendationid"]),
        author_id=str(steamid) if steamid else None,
        created_at=_utc(created),
        edited_at=_utc(max(int(raw.get("timestamp_updated") or 0), created)),
        text=raw.get("review") or "",
        voted_up=bool(raw["voted_up"]),
        language=raw.get("language") or "unknown",
        permalink=REVIEW_URL.format(steamid=steamid, app_id=app_id) if steamid else "",
        raw=raw,
    )


def comment_record(comment: Comment, app_id: int, topic_id: str, event_gid: str | None, page: int) -> FeedbackRecord:
    url = COMMENTS_URL.format(app_id=app_id, topic_id=topic_id)
    return FeedbackRecord(
        source_type="comment",
        source_item_id=comment.comment_id,
        author_id=comment.author_steamid,
        created_at=_utc(comment.created_at),
        edited_at=_utc(comment.edited_at or comment.created_at),
        text=comment.text,
        voted_up=None,
        language=detect_language(comment.text),
        permalink=f"{url}?ctp={page}#c{comment.comment_id}",
        raw={"html": comment.raw_html, "page": page},
        thread_id=topic_id,
        event_gid=event_gid,
    )


# --- Language ------------------------------------------------------------------------------------
# Comments have no language tag, so a small word-list check decides. It only rejects text that is
# clearly another language; short or ambiguous text stays in scope as "uncertain" and is counted.

_URL = re.compile(r"https?://\S+")
_EMOTICON = re.compile(r":[A-Za-z0-9_]+:")
_LETTERS = re.compile(r"[^\W\d_]+")
ENGLISH_WORDS = frozenset(
    "the a an and or but is are was were be been being it its this that these those to of in on for with "
    "not no you your i me my we our they them their he she his her have has had do does did can cant "
    "can't will would should could just so if at as by from what why how all about now still more too "
    "very really game patch update fix fixed nerf buff dont don't im i'm it's thanks please".split()
)
OTHER_WORDS = frozenset(  # frequent words in other languages that are not also English words
    "el los las del que y es por para muy pero juego esto esta sólo también "
    "der das und ist nicht ein eine mit auch noch sehr aber spiel ich wir "
    "le les des et est pas une pour avec très mais jeu je nous "
    "il che sono della questo molto gioco "
    "jest nie się że jak ale gra "
    "não com uma mais isso jogo muito".split()
)


def detect_language(text: str) -> str:
    """english, non_english or uncertain."""
    words = _LETTERS.findall(_EMOTICON.sub(" ", _URL.sub(" ", text)).lower())
    if not words:
        return "uncertain"
    letters = "".join(words)
    if sum(ch < "ɐ" for ch in letters) / len(letters) < 0.5:  # mostly non-Latin script
        return "non_english"
    english = sum(w in ENGLISH_WORDS for w in words)
    other = sum(w in OTHER_WORDS for w in words)
    if other > english:
        return "non_english"
    return "english" if english else "uncertain"


# --- Window eligibility and historical edits -----------------------------------------------------

@dataclass(frozen=True)
class StoredVersion:
    version_id: int
    edited_at: datetime
    text: str
    language: str


def check_eligibility(
    created_at: datetime, versions: list[StoredVersion], window_start: datetime, window_end: datetime
) -> tuple[StoredVersion | None, str | None]:
    """Return (version to use, exclusion reason or None).

    The window is decided by the original creation time. The text must be a version that existed
    before the window ended: the latest captured version last edited before window_end. If every
    captured version was edited at or after window_end, the current text cannot stand in for what
    was written during the window, so the item is excluded instead.
    """
    if not window_start <= created_at < window_end:
        return None, "outside_window"
    valid = [v for v in versions if v.edited_at < window_end]
    if not valid:
        return None, "edited_after_window"
    version = max(valid, key=lambda v: (v.edited_at, v.version_id))
    if version.language not in IN_SCOPE_LANGUAGES:
        return version, "non_english"
    if not version.text.strip():
        return version, "empty_text"
    return version, None

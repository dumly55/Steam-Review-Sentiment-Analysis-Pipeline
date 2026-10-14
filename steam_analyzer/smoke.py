"""Bounded live checks of the Steam sources. Makes a handful of real requests; stores nothing."""

import logging
import time
from dataclasses import dataclass

from steam_analyzer import steam_sources as steam
from steam_analyzer.http import make_session

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmokeResult:
    name: str
    ok: bool
    detail: str


def check_reviews(session, app_id: int) -> SmokeResult:
    first = steam.fetch_reviews_page(session, app_id, num_per_page=20)
    second = steam.fetch_reviews_page(session, app_id, cursor=first["cursor"], num_per_page=20)
    ids1 = {r["recommendationid"] for r in first["reviews"]}
    ids2 = {r["recommendationid"] for r in second["reviews"]}
    if not ids1 or not ids2:
        return SmokeResult("Reviews API", False, "empty page returned")
    if ids1 & ids2:
        return SmokeResult("Reviews API", False, f"{len(ids1 & ids2)} reviews repeated across pages")
    times = [r["timestamp_created"] for r in first["reviews"] + second["reviews"]]
    if times != sorted(times, reverse=True):
        return SmokeResult("Reviews API", False, "pages are not newest-first")
    return SmokeResult(
        "Reviews API", True,
        f"2 pages, {len(ids1) + len(ids2)} distinct reviews, total_matching={first.get('total_matching')}",
    )


def find_release_with_comments(session, app_id: int) -> dict | None:
    for event in steam.fetch_events_page(session, app_id):
        body = event.get("announcement_body") or {}
        # event["comment_count"] is always 0; the real count is on the announcement body.
        if event["event_type"] in steam.RELEASE_EVENT_TYPES and body.get("forum_topic_id") and body.get("commentcount"):
            return event
    return None


def check_events(session, app_id: int) -> SmokeResult:
    events = steam.fetch_events_page(session, app_id, count=20)
    releases = [e for e in events if e["event_type"] in steam.RELEASE_EVENT_TYPES]
    if not releases:
        return SmokeResult("Events API", False, f"{len(events)} events but no release types {sorted(steam.RELEASE_EVENT_TYPES)}")
    latest = releases[0]
    return SmokeResult(
        "Events API", True,
        f"{len(events)} recent events, {len(releases)} releases; latest release: {latest['event_name'].strip()!r}",
    )


def check_comments(session, app_id: int, max_pages: int = 10) -> SmokeResult:
    event = find_release_with_comments(session, app_id)
    if event is None:
        return SmokeResult("Comments", False, "no recent release event with a comment thread found")
    topic_id = event["announcement_body"]["forum_topic_id"]
    seen: dict[str, steam.Comment] = {}
    total = None
    pages = 0
    for page in range(1, max_pages + 1):
        comments, total = steam.parse_comment_page(steam.fetch_comment_page(session, app_id, topic_id, page))
        new = [c for c in comments if c.comment_id not in seen]
        if not new:
            break
        pages += 1
        seen.update((c.comment_id, c) for c in new)
        time.sleep(1)
    times = [c.created_at for c in seen.values()]
    complete = total is not None and len(seen) == total
    detail = (
        f"{event['event_name'].strip()!r}: {len(seen)} comments over {pages} page(s), "
        f"reported total {total}, {sum(c.edited_at is not None for c in seen.values())} edited, "
        f"oldest-first={times == sorted(times)}, {'complete' if complete else 'PARTIAL (page cap or mismatch)'}"
    )
    return SmokeResult("Comments", bool(seen) and times == sorted(times), detail)


def run_smoke(app_id: int) -> list[SmokeResult]:
    session = make_session()
    results = []
    for check in (check_reviews, check_events, check_comments):
        try:
            results.append(check(session, app_id))
        except Exception as exc:
            log.exception("smoke check %s failed", check.__name__)
            results.append(SmokeResult(check.__name__, False, f"error: {exc}"))
    return results

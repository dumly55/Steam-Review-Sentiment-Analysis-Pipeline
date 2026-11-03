"""Low-level access to the three public Steam sources.

Verified against live Helldivers 2 data on 2026-10-14; see docs/source_findings.md.
These functions fetch and parse single pages. Storage, checkpoints and window
filtering belong to the collectors built on top of them.
"""

import html
import json
import re
from dataclasses import dataclass

import requests

from steam_analyzer.http import request_with_retry

REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
EVENTS_URL = "https://store.steampowered.com/events/ajaxgetadjacentpartnerevents/"
NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
COMMENTS_URL = "https://steamcommunity.com/app/{app_id}/eventcomments/{topic_id}/"
# The store page takes the event gid; the announcement gid opens a generic news hub instead.
ANNOUNCEMENT_URL = "https://store.steampowered.com/news/app/{app_id}/view/{event_gid}"

REVIEW_FILTER_RECENT = 1  # the only sort that ends with an empty page, so it can enumerate everything
PURCHASE_TYPE_ALL = 1

# Steam partner event_type values observed on real data.
RELEASE_EVENT_TYPES = {12: "small_update", 13: "regular_update", 14: "major_update"}
NEWS_EVENT_TYPE = 28

STEAMID64_BASE = 76561197960265728


def fetch_reviews_page(
    session: requests.Session,
    app_id: int,
    cursor: str = "*",
    num_per_page: int = 100,
    date_range: tuple[int, int] | None = None,
    languages: tuple[str, ...] = ("english",),
) -> dict:
    """One page of reviews, newest first. Includes off-topic (review-bomb) activity."""
    params = {
        "appid": app_id,
        "filter": REVIEW_FILTER_RECENT,
        "languages": list(languages),
        "num_per_page": num_per_page,
        "cursor": cursor,
        "filter_offtopic_activity": False,
        "purchase_type": PURCHASE_TYPE_ALL,
    }
    if date_range:
        params["date_range_start"], params["date_range_end"] = date_range
    resp = request_with_retry(session, "GET", REVIEWS_URL, params={"input_json": json.dumps(params)})
    return resp.json()["response"]


def fetch_events_page(
    session: requests.Session, app_id: int, older_than_gid: str | None = None, count: int = 100
) -> list[dict]:
    """Up to `count` announcement events, newest first, starting at `older_than_gid` when given.

    The anchor event itself is included in the result, so callers must de-duplicate.
    """
    params = {"appid": app_id, "count_before": 0, "count_after": count, "lang_list": "0"}
    if older_than_gid:
        params["gidevent"] = older_than_gid
    resp = request_with_retry(session, "GET", EVENTS_URL, params=params)
    return resp.json().get("events", [])


def fetch_announcement_count(session: requests.Session, app_id: int) -> int:
    """Total official announcements according to the news API, used to cross-check catalog coverage."""
    params = {"appid": app_id, "count": 1, "maxlength": 1, "feeds": "steam_community_announcements"}
    resp = request_with_retry(session, "GET", NEWS_URL, params=params)
    return int(resp.json()["appnews"]["count"])


def fetch_comment_page(session: requests.Session, app_id: int, topic_id: str, page: int) -> str:
    """Raw HTML of one public announcement comment page (15 comments per page, oldest first)."""
    url = COMMENTS_URL.format(app_id=app_id, topic_id=topic_id)
    return request_with_retry(session, "GET", url, params={"ctp": page}).text


@dataclass(frozen=True)
class Comment:
    comment_id: str
    author_steamid: str | None
    created_at: int
    edited_at: int | None
    text: str
    raw_html: str


_COMMENT_START = re.compile(r'class="commentthread_comment [^"]*" id="comment_(\d+)"')
_TIMESTAMP = re.compile(r'class="commentthread_comment_timestamp"[^>]*data-timestamp="(\d+)"')
_AUTHOR = re.compile(r'data-miniprofile="(\d+)"')
_EDITED = re.compile(r'class="forum_audit".*?data-timestamp="(\d+)"', re.S)
_TOTAL = re.compile(r'"total_count":(\d+)')
_BLOCKQUOTE = re.compile(r"<blockquote\b.*?</blockquote>", re.S | re.I)
_IMG_ALT = re.compile(r'<img[^>]*\balt="([^"]*)"[^>]*>', re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def comment_html_to_text(fragment: str) -> str:
    """Plain text of a comment body, without text quoted from other comments."""
    fragment = _BLOCKQUOTE.sub("", fragment)
    fragment = _IMG_ALT.sub(r"\1", fragment)  # emoticons keep their :name:
    fragment = _BR.sub("\n", fragment)
    text = html.unescape(_TAG.sub("", fragment))
    return "\n".join(line.strip() for line in text.strip().splitlines()).strip()


def parse_comment_page(page_html: str) -> tuple[list[Comment], int | None]:
    """Comments on a page plus the thread's reported total_count (None if not found)."""
    starts = list(_COMMENT_START.finditer(page_html))
    comments = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(page_html)
        block = page_html[match.start():end]
        comment_id = match.group(1)
        timestamp = _TIMESTAMP.search(block)
        if not timestamp:
            raise ValueError(f"comment {comment_id} has no timestamp; page markup may have changed")
        author = _AUTHOR.search(block)
        edited = _EDITED.search(block)

        body_start = block.find(f'id="comment_content_{comment_id}">')
        body_end = block.find('class="comment_footer_ctn"', body_start)
        body = block[body_start:body_end] if body_start != -1 else ""
        body = body[body.find(">") + 1:body.rfind("</div>")] if body else ""

        comments.append(
            Comment(
                comment_id=comment_id,
                author_steamid=str(STEAMID64_BASE + int(author.group(1))) if author else None,
                created_at=int(timestamp.group(1)),
                edited_at=int(edited.group(1)) if edited else None,
                text=comment_html_to_text(body),
                raw_html=block,
            )
        )
    total = _TOTAL.search(page_html)
    return comments, int(total.group(1)) if total else None

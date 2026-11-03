"""SYNTHETIC DEMO: an invented game with invented releases, reviews, comments and scripted model answers.

Nothing here is real data or a real finding. It lets the whole pipeline run offline in seconds, without
Steam or Ollama. The game is stored with is_synthetic = TRUE and a name that says so, every screen labels
it, and the "model" is a script whose answers go through the same validation as real model output.

The data is built to show the rules at work:
- a sale post between releases that does not end a window
- general praise of the game plus criticism of a patch change (a negative patch reaction)
- one commenter posting eight times about the same feature (counted once)
- comments posted after the next release, a non-English comment, and edits made after the window
- one review whose scripted answer quotes text that is not in the review, so it fails validation
- reviews and comments that disagree, so the overall label is Mixed
"""

import functools
import html
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from steam_analyzer.catalog_store import RefreshResult, load_catalog, refresh_catalog
from steam_analyzer.collectors import Sources
from steam_analyzer.config import Settings, analysis_config
from steam_analyzer.db import delete_app_data
from steam_analyzer.steam_sources import STEAMID64_BASE

DEMO_APP_ID = 999_000_100
DEMO_NAME = "Synthetic demo game (invented data)"
SCRIPTED_MODEL = "synthetic-scripted-extractor"
REVIEW_PAGE_SIZE = 20  # small, so the demo pages through reviews
COMMENT_PAGE_SIZE = 15


def _t(day: int, hour: int = 17) -> int:
    return int(datetime(2026, 3, day, hour, tzinfo=timezone.utc).timestamp())


LAUNCH, SALE, BALANCE, HOTFIX = _t(2), _t(10), _t(16), _t(23)
HOUR, DAY = 3600, 86400

NOTES = {
    "launch": "[p]Welcome to launch day.[/p][h3]Weapons[/h3][list][*][p]Added the Railgun.[/p][/*]"
              "[*][p]Added the Eruptor.[/p][/*][/list][h3]Online[/h3][list][*][p]Matchmaking for squads of up "
              "to four players.[/p][/*][/list]",
    "balance": "[h3]Balance[/h3][p][i]Railgun[/i][/p][list][*][p]Reduced damage from 300 to 220.[/p][/*][/list]"
               "[p][i]Eruptor[/i][/p][list][*][p]Increased magazine size from 5 to 6.[/p][/*][/list]"
               "[h3]Fixes[/h3][list][*][p]Fixed a crash when a mission ends.[/p][/*][/list]",
    "hotfix": "[list][*][p]Fixed the Eruptor reload sound playing twice.[/p][/*][/list]",
}


@dataclass(frozen=True)
class Template:
    """A piece of feedback and the answer the scripted model gives for it."""

    text: str
    voted_up: bool | None
    overall: str
    relevance: str
    observations: tuple[dict, ...] = ()

    def answer(self, ref: str) -> dict:
        return {"feedback_id": ref, "overall_sentiment": self.overall, "update_relevance": self.relevance,
                "observations": list(self.observations), "uncertainty_reason": ""}


def _obs(key: str, category: str, sentiment: str, evidence: str, change: str = "",
         relevance: str = "explicit_update") -> dict:
    return {"feature_key": key, "category": category, "sentiment": sentiment, "relevance": relevance,
            "evidence": evidence, "change_id": change}


# Change IDs follow render_notes: balance notes are C1 railgun damage, C2 Eruptor magazine, C3 crash fix.
NERF_BUT_FUN = Template(
    "The railgun nerf ruined my favourite weapon. Still a fun game with friends though.", True, "mixed",
    "explicit_update", (_obs("railgun", "balance", "negative", "The railgun nerf ruined my favourite weapon", "C1"),
                        _obs("co_op", "content", "positive", "Still a fun game with friends", relevance="general_game")))
NERF = Template("Railgun damage got cut way too hard in 1.1.0, it feels useless now.", False, "negative",
                "explicit_update", (_obs("railgun", "balance", "negative", "Railgun damage got cut way too hard", "C1"),))
CRASH_FIXED = Template("Finally no more crashes at the end of missions, thank you!", True, "positive", "explicit_update",
                       (_obs("crashes", "stability", "positive", "no more crashes at the end of missions", "C3"),))
CRASH_FIXED_SYNONYM = Template(  # "game_crashes" is mapped to "crashes" by the alias list in config/analysis.json
    "Game crashes after extraction are gone since the patch.", True, "positive", "explicit_update",
    (_obs("game_crashes", "stability", "positive", "Game crashes after extraction are gone", "C3"),))
ERUPTOR_BUFF = Template("Love the bigger Eruptor magazine.", True, "positive", "explicit_update",
                        (_obs("eruptor", "balance", "positive", "Love the bigger Eruptor magazine", "C2"),))
SPLIT = Template("The Eruptor buff is nice but the railgun change is not.", True, "mixed", "explicit_update",
                 (_obs("eruptor", "balance", "positive", "The Eruptor buff is nice", "C2"),
                  _obs("railgun", "balance", "negative", "the railgun change is not", "C1")))
GENERAL = Template("Great game, 200 hours in and still having fun.", True, "positive", "general_game")
BAD_QUOTE = Template("10/10 would dive again", True, "positive", "general_game",
                     (_obs("replayability", "content", "positive", "best game of the year", relevance="general_game"),))

LAUNCH_MATCHMAKING = Template(
    "Launch went smoothly and matchmaking works great.", True, "positive", "explicit_update",
    (_obs("matchmaking", "matchmaking", "positive", "matchmaking works great", "C3"),))
LAUNCH_RAILGUN = Template("The Railgun is so much fun to use.", True, "positive", "plausibly_related",
                          (_obs("railgun", "balance", "positive", "The Railgun is so much fun to use", "C1",
                                "plausibly_related"),))
LAUNCH_CRASHES = Template("Crashes at the end of every mission, please fix.", False, "negative", "general_game",
                          (_obs("crashes", "stability", "negative", "Crashes at the end of every mission",
                                relevance="general_game"),))
HOTFIX_SOUND = Template("Reload sound is fixed, nice.", True, "positive", "explicit_update",
                        (_obs("eruptor_reload_sound", "bugs", "positive", "Reload sound is fixed", "C1"),))
STILL_WAITING = Template("Still waiting for the railgun to be fixed.", False, "negative", "plausibly_related",
                         (_obs("railgun", "balance", "negative", "waiting for the railgun to be fixed",
                               relevance="plausibly_related"),))

REVERT_NERF = [f"{prefix} the railgun nerf." for prefix in (
    "Please revert", "Seriously, revert", "Still asking you to revert", "Day three, revert",
    "Nobody asked for", "I can't enjoy the game after", "One more time: undo", "Please, please undo")]
CRASH_THANKS = Template("Thanks for fixing the end-of-mission crash!", None, "positive", "explicit_update",
                        (_obs("crashes", "stability", "positive", "Thanks for fixing the end-of-mission crash", "C3"),))
SIX_ROUNDS = Template("Six rounds in the Eruptor feels great.", None, "positive", "explicit_update",
                      (_obs("eruptor", "balance", "positive", "Six rounds in the Eruptor feels great", "C2"),))
RAILGUN_WEAK = Template("Railgun feels weak after this patch.", None, "negative", "explicit_update",
                        (_obs("railgun", "balance", "negative", "Railgun feels weak after this patch", "C1"),))
GOOD_PATCH = Template("Good patch overall.", None, "positive", "explicit_update",
                      (_obs("update_overall", "other", "positive", "Good patch overall"),))
SPANISH = Template("El juego es muy bueno pero el railgun es malo ahora.", None, "mixed", "explicit_update")
NEXT_PATCH = Template("Any news on the next patch?", None, "neutral", "unrelated")
LAUNCH_HYPE = Template("Day one and the game is great, well done.", None, "positive", "explicit_update",
                       (_obs("update_overall", "other", "positive", "the game is great, well done"),))


@dataclass(frozen=True)
class DemoComment:
    comment_id: str
    account: int | None
    created_at: int
    edited_at: int | None
    text: str


@dataclass
class DemoData:
    events: list[dict]
    reviews: list[dict] = field(default_factory=list)
    comments: dict[str, list[DemoComment]] = field(default_factory=dict)
    answers: dict[str, dict] = field(default_factory=dict)


def _event(key: str, ts: int, title: str, event_type: int, body: str, comments: int) -> dict:
    return {"gid": f"demo-e-{key}", "event_type": event_type, "event_name": title, "rtime32_start_time": ts,
            "hidden": 0, "announcement_body": {"gid": f"demo-a-{key}", "posttime": ts, "updatetime": ts,
                                               "forum_topic_id": f"demo-t-{key}", "body": body,
                                               "commentcount": comments}}


@functools.cache
def build_demo_data() -> DemoData:
    data = DemoData(events=[])
    accounts = itertools.count(1001)
    review_ids = itertools.count(8_000_001)
    comment_ids = itertools.count(7_000_001)

    def review(created: int, t: Template, edited_at: int | None = None) -> None:
        rid = str(next(review_ids))
        data.reviews.append({
            "recommendationid": rid, "author": {"steamid": str(STEAMID64_BASE + next(accounts))},
            "language": "english", "review": t.text, "timestamp_created": created,
            "timestamp_updated": edited_at or created, "voted_up": t.voted_up,
        })
        data.answers[f"review:{rid}"] = t.answer(f"review:{rid}")

    def comment(thread: str, created: int, t: Template, account: int | None = -1, edited_at: int | None = None,
                text: str | None = None) -> None:
        cid = str(next(comment_ids))
        data.comments.setdefault(f"demo-t-{thread}", []).append(DemoComment(
            cid, next(accounts) if account == -1 else account, created, edited_at, text or t.text))
        answer = t.answer(f"comment:{cid}")
        if text:
            answer["observations"] = [_obs("railgun", "balance", "negative", text.rstrip("."), "C1")]
        data.answers[f"comment:{cid}"] = answer

    def spread(start: int, templates: list[tuple[Template, int]]) -> list[tuple[int, Template]]:
        flat = [t for t, n in templates for _ in range(n)]
        return [(start + (i + 1) * HOUR, t) for i, t in enumerate(flat)]

    # Releases, plus a sale post and a duplicate hotfix post that must not create boundaries.
    data.events = [
        _event("launch", LAUNCH, "Demo Launch 1.0.0", 14, NOTES["launch"], 8),
        _event("sale", SALE, "Weekend sale: 30% off", 28, "[p]Everything is on sale.[/p]", 0),
        _event("balance", BALANCE, "Demo Balance Update 1.1.0", 13, NOTES["balance"], 35),
        _event("hotfix", HOTFIX, "Demo Hotfix 1.1.1", 12, NOTES["hotfix"], 3),
        _event("hotfix-post", HOTFIX + 2 * HOUR, "The hotfix is live", 12, NOTES["hotfix"], 2),
    ]

    # Launch window: positive patch reactions; the crash complaints do not tie themselves to the update.
    for created, t in spread(LAUNCH, [(LAUNCH_MATCHMAKING, 14), (LAUNCH_RAILGUN, 9), (LAUNCH_CRASHES, 5)]):
        review(created, t)
    for created, t in spread(LAUNCH, [(LAUNCH_HYPE, 8)]):
        comment("launch", created, t)

    # Balance window: reviews lean negative, comments lean positive, so the overall label is Mixed.
    for created, t in spread(BALANCE, [(NERF_BUT_FUN, 14), (NERF, 10), (CRASH_FIXED, 4), (CRASH_FIXED_SYNONYM, 2),
                                       (ERUPTOR_BUFF, 5), (SPLIT, 4), (GENERAL, 5), (BAD_QUOTE, 1)]):
        review(created, t)
    review(BALANCE + 2 * DAY, NERF, edited_at=HOTFIX + DAY)  # edited after the window: excluded

    persistent = next(accounts)  # one person, eight comments about the same change
    for i, text in enumerate(REVERT_NERF):
        comment("balance", BALANCE + (i + 1) * 5 * HOUR, RAILGUN_WEAK, account=persistent, text=text)
    for created, t in spread(BALANCE + 30 * 60, [(CRASH_THANKS, 14), (SIX_ROUNDS, 5), (RAILGUN_WEAK, 3)]):
        comment("balance", created, t)
    comment("balance", BALANCE + 3 * DAY, GOOD_PATCH, account=None)          # author not shown
    comment("balance", BALANCE + 3 * DAY + HOUR, SPANISH)                    # outside the language scope
    comment("balance", BALANCE + 4 * DAY, CRASH_THANKS, edited_at=HOTFIX + HOUR)  # edited after the window
    comment("balance", HOTFIX + HOUR, NEXT_PATCH)                            # posted after the next release
    comment("balance", HOTFIX + 2 * DAY, NEXT_PATCH)
    for thread in data.comments.values():
        thread.sort(key=lambda c: c.created_at)

    # Hotfix window (ongoing): too little feedback for a label.
    for created, t in spread(HOTFIX, [(HOTFIX_SOUND, 4), (STILL_WAITING, 2)]):
        review(created, t)
    for created, t in spread(HOTFIX + 30 * 60, [(HOTFIX_SOUND, 3)]):
        comment("hotfix", created, t)
    for created, t in spread(HOTFIX + 3 * HOUR, [(HOTFIX_SOUND, 2)]):
        comment("hotfix-post", created, t)
    return data


# --- Synthetic sources and scripted extractor ----------------------------------------------------

def render_comment_page(comments: list[DemoComment], total: int) -> str:
    """HTML in the same structure as a real Steam comment page, so the real parser reads it."""
    blocks = []
    for c in comments:
        author = f' data-miniprofile="{c.account}"' if c.account else ""
        edited = (f'<div class="forum_audit">Last edited <span data-timestamp="{c.edited_at}">edited</span></div>'
                  if c.edited_at else "")
        blocks.append(
            f'<div class="commentthread_comment responsive_body_text   " id="comment_{c.comment_id}">'
            f'<a class="hoverunderline commentthread_author_link"{author}><bdi>Player</bdi></a>'
            f'<div class="commentthread_comment_timestamp" title="" data-timestamp="{c.created_at}">time</div>'
            f'<div class="commentthread_comment_text" id="comment_content_{c.comment_id}">{html.escape(c.text)}</div>'
            f'<div class="comment_footer_ctn">{edited}</div></div>')
    header = ('<script>InitializeCommentThread("ForumTopic", "demo", '
              f'{{"total_count":{total},"start":0,"pagesize":{COMMENT_PAGE_SIZE}}});</script>')
    return header + "\n".join(blocks)


def demo_sources() -> Sources:
    data = build_demo_data()

    def fetch_reviews(cursor: str, date_range: tuple[int, int]) -> dict:
        start, end = date_range
        matching = sorted((r for r in data.reviews if start <= r["timestamp_created"] <= end),
                          key=lambda r: -r["timestamp_created"])
        offset = 0 if cursor == "*" else int(cursor)
        page = matching[offset:offset + REVIEW_PAGE_SIZE]
        return {"reviews": page, "cursor": str(offset + len(page)), "total_matching": len(matching)}

    def fetch_comment_page(thread_id: str, page: int) -> str:
        thread = data.comments.get(thread_id, [])
        return render_comment_page(thread[(page - 1) * COMMENT_PAGE_SIZE:page * COMMENT_PAGE_SIZE], len(thread))

    return Sources(fetch_reviews, fetch_comment_page)


class ScriptedExtractor:
    """Returns the prepared answer for each feedback ID. Not a model; never presented as one."""

    model = SCRIPTED_MODEL
    model_digest = "synthetic"
    max_attempts = 2

    def __init__(self, answers: dict[str, dict], max_feedback_chars: int):
        self.answers = answers
        self.settings = {"scripted": True, "max_feedback_chars": max_feedback_chars}

    def complete(self, messages: list[dict], schema: dict) -> tuple[str, int | None]:
        ref = re.search(r"FEEDBACK ID: (\S+)", messages[1]["content"]).group(1)
        return json.dumps(self.answers[ref]), None


def make_scripted_extractor(config: dict) -> ScriptedExtractor:
    return ScriptedExtractor(build_demo_data().answers, config["analysis"]["extraction"]["max_feedback_chars"])


# --- Loading the demo ----------------------------------------------------------------------------

def demo_analysis(settings: Settings) -> dict:
    config = analysis_config(settings)
    config["model"] = SCRIPTED_MODEL
    config["collection"].update(page_delay_seconds=0, review_page_size=REVIEW_PAGE_SIZE)
    return config


def refresh_demo_catalog(conn: psycopg.Connection) -> RefreshResult:
    events = build_demo_data().events
    ordered = sorted(events, key=lambda e: -e["rtime32_start_time"])
    return refresh_catalog(conn, DEMO_APP_ID,
                           lambda anchor: ordered if anchor is None else [e for e in ordered if e["gid"] == anchor],
                           fetch_news_count=lambda: len(events))


def load_demo(conn: psycopg.Connection) -> None:
    """Replace any earlier demo data with a fresh synthetic catalog. Touches only the demo app id."""
    delete_app_data(conn, DEMO_APP_ID)
    conn.execute("INSERT INTO games (app_id, name, is_synthetic) VALUES (%s, %s, TRUE)", (DEMO_APP_ID, DEMO_NAME))
    conn.commit()
    refresh_demo_catalog(conn)
    load_catalog(conn, DEMO_APP_ID)

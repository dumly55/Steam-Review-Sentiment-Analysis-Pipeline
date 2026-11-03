"""The synthetic demo must stay honest: its scripted answers pass the same validation as model output,
and its comment pages are read by the real parser."""

import json

import pytest

from steam_analyzer.config import analysis_config, load_settings
from steam_analyzer.demo import BAD_QUOTE, build_demo_data, demo_sources
from steam_analyzer.extraction import Context, InvalidOutput, parse_output
from steam_analyzer.steam_sources import parse_comment_page

DATA = build_demo_data()
CTX = Context("demo", "", frozenset({"C1", "C2", "C3"}), tuple(analysis_config(load_settings())["categories"]), ())


def texts() -> dict[str, str]:
    found = {f"review:{r['recommendationid']}": r["review"] for r in DATA.reviews}
    found.update({f"comment:{c.comment_id}": c.text for thread in DATA.comments.values() for c in thread})
    return found


def test_every_scripted_answer_validates_except_the_deliberate_bad_quote():
    failures = []
    for ref, text in texts().items():
        try:
            parse_output(json.dumps(DATA.answers[ref]), ref, text, CTX)
        except InvalidOutput:
            failures.append(text)
    assert failures == [BAD_QUOTE.text]


def test_synthetic_comment_pages_parse_with_the_real_parser():
    fetch = demo_sources().fetch_comment_page
    comments, total = parse_comment_page(fetch("demo-t-balance", 1))
    assert total == len(DATA.comments["demo-t-balance"]) and len(comments) == 15
    page3, _ = parse_comment_page(fetch("demo-t-balance", 3))
    assert any(c.edited_at for c in page3) and any(c.author_steamid is None for c in page3)
    assert parse_comment_page(fetch("demo-t-balance", 4))[0] == []


@pytest.mark.parametrize("thread", ["demo-t-launch", "demo-t-balance", "demo-t-hotfix", "demo-t-hotfix-post"])
def test_every_demo_comment_round_trips_through_the_page_markup(thread):
    fetch = demo_sources().fetch_comment_page
    parsed = [c for page in range(1, 4) for c in parse_comment_page(fetch(thread, page))[0]]
    assert [(c.comment_id, c.text) for c in parsed] == [(c.comment_id, c.text) for c in DATA.comments[thread]]

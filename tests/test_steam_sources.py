from pathlib import Path

import pytest

from steam_analyzer.steam_sources import STEAMID64_BASE, comment_html_to_text, parse_comment_page

FIXTURE = (Path(__file__).parent / "fixtures" / "comment_page_synthetic.html").read_text(encoding="utf-8")


def test_parses_ids_authors_and_timestamps():
    comments, total = parse_comment_page(FIXTURE)
    assert total == 17
    assert [c.comment_id for c in comments] == ["1001", "1002"]
    assert [c.created_at for c in comments] == [1790240436, 1790240700]
    assert comments[0].author_steamid == str(STEAMID64_BASE + 100)


def test_edit_timestamp_is_captured_only_when_shown():
    first, second = parse_comment_page(FIXTURE)[0]
    assert first.edited_at == 1790245081
    assert second.edited_at is None


def test_quoted_text_from_other_comments_is_removed():
    first = parse_comment_page(FIXTURE)[0][0]
    assert "terrible" not in first.text
    assert first.text == "I disagree, the new & improved scope is great\n\n:steamhappy:"


def test_plain_comment_text():
    assert parse_comment_page(FIXTURE)[0][1].text == "Crashes every mission since 7.1.1"


def test_page_without_comments_is_empty_not_an_error():
    assert parse_comment_page("<html></html>") == ([], None)


def test_missing_timestamp_fails_loudly():
    broken = FIXTURE.replace('data-timestamp="1790240700"', "")
    with pytest.raises(ValueError, match="1002"):
        parse_comment_page(broken)


def test_comment_html_to_text_strips_tags():
    assert comment_html_to_text("a<br>b <b>c</b> &lt;d&gt;") == "a\nb c <d>"

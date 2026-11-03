from datetime import datetime, timedelta, timezone

from steam_analyzer.feedback import StoredVersion, check_eligibility, comment_record, detect_language, review_record
from steam_analyzer.steam_sources import Comment

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
START, END = T0, T0 + timedelta(days=10)
SEC = timedelta(seconds=1)


def v(version_id: int, edited_at: datetime, text: str = "the patch is fine", language: str = "english") -> StoredVersion:
    return StoredVersion(version_id, edited_at, text, language)


# --- Window boundaries (AC04, AC07) -----------------------------------------------------------------

def test_feedback_at_the_window_start_is_included():
    assert check_eligibility(START, [v(1, START)], START, END) == (v(1, START), None)


def test_feedback_at_the_next_release_belongs_to_the_newer_update_only():
    assert check_eligibility(END, [v(1, END)], START, END) == (None, "outside_window")
    assert check_eligibility(END - SEC, [v(1, END - SEC)], START, END)[1] is None


def test_feedback_before_the_release_is_excluded():
    assert check_eligibility(START - SEC, [v(1, START - SEC)], START, END) == (None, "outside_window")


# --- Historical edits (AC13) -----------------------------------------------------------------------

def test_text_edited_during_the_window_uses_the_edited_version():
    versions = [v(1, START + SEC, "first draft"), v(2, END - SEC, "final words")]
    assert check_eligibility(START + SEC, versions, START, END)[0].text == "final words"


def test_text_edited_after_the_window_uses_an_archived_version_when_one_was_captured():
    versions = [v(1, START + SEC, "written during the window"), v(2, END + timedelta(days=30), "rewritten later")]
    version, reason = check_eligibility(START + SEC, versions, START, END)
    assert reason is None and version.text == "written during the window"


def test_text_edited_after_the_window_without_an_archive_is_excluded_not_analyzed():
    versions = [v(2, END + timedelta(days=30), "rewritten later")]
    assert check_eligibility(START + SEC, versions, START, END) == (None, "edited_after_window")


def test_an_edit_exactly_at_the_window_end_counts_as_after_it():
    assert check_eligibility(START + SEC, [v(1, END)], START, END) == (None, "edited_after_window")


# --- Language and text --------------------------------------------------------------------------

def test_out_of_scope_language_and_empty_text_are_excluded_with_the_version_kept():
    assert check_eligibility(START, [v(1, START, language="schinese")], START, END)[1] == "non_english"
    assert check_eligibility(START, [v(1, START, language="non_english")], START, END)[1] == "non_english"
    version, reason = check_eligibility(START, [v(1, START, text="   ")], START, END)
    assert (version.version_id, reason) == (1, "empty_text")


def test_uncertain_language_stays_in_scope():
    assert check_eligibility(START, [v(1, START, text="o7", language="uncertain")], START, END)[1] is None


def test_language_check_for_comments():
    assert detect_language("The railgun nerf is not fun at all") == "english"
    assert detect_language("El juego es muy bueno pero el railgun es malo ahora.") == "non_english"
    assert detect_language("Das Spiel ist nicht mehr gut") == "non_english"
    assert detect_language("Игра стала хуже после патча") == "non_english"
    assert detect_language("based") == "uncertain"           # too short to tell, kept and counted
    assert detect_language(":steamhappy: :steamhappy:") == "uncertain"
    assert detect_language("the railgun is muy malo now lol") == "english"


# --- Records ---------------------------------------------------------------------------------------

def test_review_record_keeps_recommendation_and_edit_time_separately():
    raw = {"recommendationid": "42", "author": {"steamid": "76561198000000001"}, "language": "english",
           "review": "Good", "timestamp_created": 1_700_000_000, "timestamp_updated": 1_700_000_500, "voted_up": False}
    record = review_record(raw, 553850)
    assert (record.source_item_id, record.voted_up, record.language) == ("42", False, "english")
    assert record.edited_at - record.created_at == timedelta(seconds=500)
    assert record.permalink == "https://steamcommunity.com/profiles/76561198000000001/recommended/553850/"


def test_unedited_comment_uses_its_creation_time_as_edit_time():
    comment = Comment("7", None, 1_700_000_000, None, "Crashes every mission", "<div/>")
    record = comment_record(comment, 553850, "999", "123", page=2)
    assert record.edited_at == record.created_at and record.author_id is None
    assert record.permalink.endswith("/eventcomments/999/?ctp=2#c7")
    assert (record.thread_id, record.event_gid, record.voted_up) == ("999", "123", None)

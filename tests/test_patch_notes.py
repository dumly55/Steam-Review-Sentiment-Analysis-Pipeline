from steam_analyzer.patch_notes import TRUNCATION_NOTE, render_notes

# Markup copied from the real Helldivers 2 7.1.1 notes (2026-09-24).
HOTFIX_711 = (
    "[p]Hotfix aimed at resolving some crashes for players when using terminals, or users with superwide windows.[/p]"
    "[p][/p][h3]🔧 [b]Fixes[/b][/h3][p][/p][p][b]Crash Fixes[/b][/p][list][*][p]Fixed crash that could occur in "
    "Fullscreen when opening terminals. [/p][/*][*][p]Fixed crash that could occur with superwide windows.[/p][/*]"
    "[/list][p]\n[/p][p]🧠[b] [/b][url=\"https://example.invalid/known-issues\" style=\"pill\"]Known Issues[/url][/p]"
)


def test_bullets_become_tagged_changes_under_their_heading_and_subject():
    notes = render_notes(HOTFIX_711, 10_000)
    assert [(c.change_id, c.section, c.text) for c in notes.changes] == [
        ("C1", "🔧 Fixes / Crash Fixes", "Fixed crash that could occur in Fullscreen when opening terminals."),
        ("C2", "🔧 Fixes / Crash Fixes", "Fixed crash that could occur with superwide windows."),
    ]
    assert notes.text.splitlines() == [
        "Hotfix aimed at resolving some crashes for players when using terminals, or users with superwide windows.",
        "## 🔧 Fixes",
        "### Crash Fixes",
        "[C1] Fixed crash that could occur in Fullscreen when opening terminals.",
        "[C2] Fixed crash that could occur with superwide windows.",
        "🧠 Known Issues",
    ]
    assert not notes.truncated


def test_a_new_subject_line_starts_a_new_section_for_later_changes():
    body = ("[h3]Stratagems[/h3][p][i]AMW-20 APEX[/i][/p][list][*][p]Decreased caliber from 60 to 20[/p][/*][/list]"
            "[p][i]LAS-44 NOVA[/i][/p][list][*][p]Fixed armor penetration display[/p][/*][/list]")
    assert [c.section for c in render_notes(body, 10_000).changes] == ["Stratagems / AMW-20 APEX",
                                                                       "Stratagems / LAS-44 NOVA"]


def test_long_notes_are_cut_at_a_line_and_flagged():
    body = "[list]" + "".join(f"[*][p]Change number {i} with some words.[/p][/*]" for i in range(100)) + "[/list]"
    notes = render_notes(body, 400)
    assert notes.truncated
    assert notes.text.endswith(TRUNCATION_NOTE)
    assert len(notes.text) <= 400 + len(TRUNCATION_NOTE) + 1
    assert all(f"[{c.change_id}]" in notes.text for c in notes.changes)  # only changes the model can see
    assert len(notes.changes) < 100


def test_notes_without_bullets_have_text_but_no_change_ids():
    notes = render_notes("[p]We fixed several crashes and tuned the economy.[/p]", 10_000)
    assert notes.text == "We fixed several crashes and tuned the economy." and notes.changes == []

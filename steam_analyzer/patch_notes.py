"""Turn Steam's BBCode patch notes into the plain text the model sees, with an ID on every listed change.

Example: "[h3]Fixes[/h3][p][b]Crash Fixes[/b][/p][list][*][p]Fixed crash in terminals.[/p][/*][/list]" becomes

    ## Fixes
    ### Crash Fixes
    [C1] Fixed crash in terminals.

Each bullet is one change. Its section is the nearest heading plus the nearest bold/italic subject line
(a weapon or system name in these notes). Pure functions only.
"""

import re
from dataclasses import dataclass

TRUNCATION_NOTE = "[The rest of the patch notes was cut to fit the model's input limit.]"

_DROP = re.compile(r"\[(img|previewyoutube|video)\b[^\]]*\].*?\[/\1\]", re.S | re.I)
_URL = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.S | re.I)
_HEADING = re.compile(r"\[h[1-6]\](.*?)\[/h[1-6]\]", re.S | re.I)
_SUBJECT = re.compile(r"\[p\]\s*\[(b|i|u)\]([^\[\]]+?)\[/\1\]\s*\[/p\]", re.I)  # a paragraph that is only bold/italic text
_BULLET = re.compile(r"\[\*\]\s*(?:\[p\])?", re.I)
_BREAK = re.compile(r"\[/?(?:p|list|olist|br|hr|table|tr)\b[^\]]*\]|\[/\*\]", re.I)
_TAG = re.compile(
    r"\[/?(?:b|i|u|s|strike|spoiler|noparse|code|quote|td|th|url|img|h[1-6]|emoticon|dynamiclink|carousel)\b[^\]]*\]",
    re.I,
)
HEADING, SUBJECT, ITEM = "\x01", "\x02", "\x03"  # line markers used while parsing


@dataclass(frozen=True)
class Change:
    change_id: str
    section: str
    text: str


@dataclass(frozen=True)
class NotesContext:
    text: str
    changes: list[Change]
    truncated: bool


def render_notes(body: str, max_chars: int) -> NotesContext:
    text = _DROP.sub("", body)
    text = _URL.sub(r"\1", text)
    text = _HEADING.sub(lambda m: f"\n{HEADING}{m.group(1)}\n", text)
    text = _SUBJECT.sub(lambda m: f"\n{SUBJECT}{m.group(2)}\n", text)
    text = _BULLET.sub(f"\n{ITEM}", text)
    text = _TAG.sub("", _BREAK.sub("\n", text))

    lines: list[tuple[str, Change | None]] = []
    heading = subject = ""
    for raw in text.split("\n"):
        marker = raw[:1] if raw[:1] in (HEADING, SUBJECT, ITEM) else ""
        content = " ".join(raw.lstrip(HEADING + SUBJECT + ITEM).split())
        if not content:
            continue
        if marker == HEADING:
            heading, subject = content, ""
            lines.append((f"## {content}", None))
        elif marker == SUBJECT:
            subject = content
            lines.append((f"### {content}", None))
        elif marker == ITEM:
            change = Change(f"C{sum(c is not None for _, c in lines) + 1}",
                            " / ".join(p for p in (heading, subject) if p), content)
            lines.append((f"[{change.change_id}] {content}", change))
        else:
            lines.append((content, None))

    kept, size = [], 0
    for line, change in lines:
        if size + len(line) + 1 > max_chars:
            break
        kept.append((line, change))
        size += len(line) + 1
    truncated = len(kept) < len(lines)
    rendered = [line for line, _ in kept] + ([TRUNCATION_NOTE] if truncated else [])
    return NotesContext("\n".join(rendered), [c for _, c in kept if c is not None], truncated)

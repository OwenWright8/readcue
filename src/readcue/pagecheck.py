"""Enough of the pages around a proposed chapter for a person to judge whether it's cut in the right place.

For a chapter running from page S to page E this shows the page before it, its first page, its last page and the
page after it, with a note where something looks off: the chapter's heading is on the page before S, page E+1 isn't
the start of anything new, or pages in the range have no readable text.
"""

from __future__ import annotations

from .detect import chapter_marks, is_back_matter, is_contents_page
from .extract import MIN_PAGE_CHARS

HEAD_LINES = 8
TAIL_LINES = 6
FULL_TEXT_CHARS = 3500
MAX_BLANK_LISTED = 12


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _card(role: str, page: int, text: str, number: int, page_count: int) -> dict:
    lines = _lines(text)
    marks = sorted(chapter_marks(text, set()))
    blank = len(text.strip()) < MIN_PAGE_CHARS
    tone, note = None, ""

    if role == "first":
        if blank:
            tone, note = "warn", "No readable text on this page."
        elif number in marks:
            tone, note = "ok", f"Opens Chapter {number}."
        elif marks:
            tone, note = "warn", f"This page opens Chapter {marks[0]}, not Chapter {number}."
        else:
            note = "No “Chapter” heading here. Fine if this book marks chapters by title; check it's the right page."
    elif role == "before":
        if number in marks:
            tone, note = "warn", f"Chapter {number}'s heading is on this page. Should the chapter start here?"
    elif role == "last":
        if blank:
            tone, note = "warn", "No readable text on this page."
    elif role == "after":
        if blank:
            tone, note = "warn", "No readable text on this page."
        elif number in marks:
            tone, note = "warn", f"This page opens Chapter {number} itself. Does the chapter end later?"
        elif marks:
            tone, note = "ok", f"Opens Chapter {marks[0]}: looks like the next chapter."
        elif is_back_matter(text):
            tone, note = "ok", "Back matter starts here."
        elif is_contents_page(text):
            tone, note = "warn", "This looks like a contents page."
        else:
            tone, note = (
                "warn",
                "This doesn't look like the start of something new. Is the chapter cut short?",
            )

    return {
        "role": role,
        "page": page,
        "head": lines[:HEAD_LINES],
        "tail": lines[-TAIL_LINES:],
        "text": text[:FULL_TEXT_CHARS],
        "truncated": len(text) > FULL_TEXT_CHARS,
        "tone": tone,
        "note": note,
    }


def check_range(pages: dict[int, str], page_count: int, start: int, end: int, number: int) -> dict:
    """`pages` maps page number to text and must cover start-1 .. end+1 (as far as they exist in the book)."""
    if not 1 <= start <= end <= page_count:
        raise ValueError(f"Pages {start} to {end} don't fit a {page_count}-page book.")

    def card(role: str, page: int) -> dict | None:
        if not 1 <= page <= page_count:
            if role == "after":  # the chapter runs to the last page of the book
                return {"role": "after", "page": None, "head": [], "tail": [], "text": "", "truncated": False,
                        "tone": "ok", "note": "That's the end of the book."}  # fmt: skip
            return None
        return _card(role, page, pages.get(page, ""), number, page_count)

    in_range = range(start, end + 1)
    blank = [p for p in in_range if len(pages.get(p, "").strip()) < MIN_PAGE_CHARS]
    warnings = []
    if blank:
        listed = ", ".join(map(str, blank[:MAX_BLANK_LISTED])) + (
            "…" if len(blank) > MAX_BLANK_LISTED else ""
        )
        warnings.append(
            f"{len(blank)} page{'s' if len(blank) != 1 else ''} in this range have no readable text ({listed})."
        )
    # A page inside the range that opens a *different* chapter means the range runs too far.
    for p in range(start + 1, end + 1):
        text = pages.get(p, "")
        marks = chapter_marks(text, set())
        if marks and number not in marks and not is_contents_page(text):
            warnings.append(f"Page {p} opens Chapter {min(marks)}. Is that really inside this chapter?")
            break
    cards = [
        c
        for c in (card("before", start - 1), card("first", start), card("last", end), card("after", end + 1))
        if c
    ]
    return {
        "page_count": page_count,
        "pages": end - start + 1,
        "words": sum(len(pages.get(p, "").split()) for p in in_range),
        "warnings": warnings,
        "cards": cards,
    }

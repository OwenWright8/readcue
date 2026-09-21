"""Find where chapters start and end inside a whole textbook, from its page texts.

The approach mirrors how a person would do it: look for the page that opens chapter N (a "Chapter 7" heading
near the top, or the chapter's title), and end the chapter on the page before chapter N+1 opens. Signals,
strongest first:

1. The PDF's own bookmarks (outline), when it has them.
2. A "Chapter N" heading in the first lines of a page. Table-of-contents pages are recognised and skipped, and
   headings are taken in book order, so a stray "see Chapter 7" near the top of an earlier page is ignored.
3. The chapter's title (from the syllabus) in the first lines of a page, for books whose headings don't say
   "Chapter".

Chapter N ends at the page before the next chapter opens, or before back matter (appendix, glossary, index...).
Everything found is only a proposal: the UI shows the ranges for the user to confirm or correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOP_LINES = 8  # a chapter opener has its heading within the first few lines of the page
TOC_SCAN_LINES = 45
MAX_HEADING_CHARS = 90
LONG_CHAPTER_PAGES = 120

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
_CHAPTER = re.compile(r"^[\W\d_]{0,14}(?:chapter|chap\.?|ch\.?)\s*(\d{1,3}|[ivxlc]{1,6})\b", re.I)
_NUMBERED = re.compile(r"^\s*(\d{1,3})\s*[.:)\-–—]?\s+[A-Za-z]")  # "7 Cell Membranes", "7. Cell ..."
_BACK_MATTER = re.compile(
    r"^\W*(appendix|appendices|glossary|index|references|bibliography|answers?\b|solutions?\b|credits)", re.I
)
_ENDS_WITH_PAGE_NUMBER = re.compile(r"\s\.{0,}\s*\d{1,4}\s*$")


@dataclass
class Found:
    number: int
    title: str = ""
    start: int | None = None  # 1-based page numbers, inclusive
    end: int | None = None
    source: str = "none"  # bookmark | heading | title | none
    snippet: str = ""  # the first lines of the opening page, so the user can recognise it
    warnings: list[str] = field(default_factory=list)

    @property
    def pages(self) -> int | None:
        return None if self.start is None or self.end is None else self.end - self.start + 1


def _lines(text: str, limit: int) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(line)
            if len(out) == limit:
                break
    return out


def _roman(token: str) -> int | None:
    token = token.lower()
    if not token or any(c not in _ROMAN for c in token):
        return None
    total = 0
    for i, c in enumerate(token):
        value = _ROMAN[c]
        total += -value if i + 1 < len(token) and _ROMAN[token[i + 1]] > value else value
    return total or None


def _number(token: str) -> int | None:
    return int(token) if token.isdigit() else _roman(token)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_contents_page(text: str) -> bool:
    """A table of contents or chapter list: many chapter lines, or many lines ending in page numbers."""
    lines = _lines(text, TOC_SCAN_LINES)
    chapter_lines = sum(1 for line in lines if _CHAPTER.match(line))
    numbered_lines = sum(1 for line in lines if _ENDS_WITH_PAGE_NUMBER.search(line))
    return chapter_lines >= 4 or numbered_lines >= 8


def is_back_matter(text: str) -> bool:
    """Does this page open an appendix, glossary, index and the like?"""
    return any(_BACK_MATTER.match(line) and len(line) < 40 for line in _lines(text, 3))


def chapter_marks(text: str, wanted: set[int]) -> set[int]:
    """Chapter numbers whose heading appears in the first lines of this page."""
    marks = set()
    for line in _lines(text, TOP_LINES):
        if len(line) > MAX_HEADING_CHARS:
            continue
        if m := _CHAPTER.match(line):
            if (n := _number(m.group(1))) is not None:
                marks.add(n)
        elif (m := _NUMBERED.match(line)) and int(m.group(1)) in wanted:
            marks.add(int(m.group(1)))
    return marks


def _outline_number(title: str) -> int | None:
    if m := _CHAPTER.match(title):
        return _number(m.group(1))
    if m := re.match(r"^\s*(\d{1,3})\s*[.:)\-–—]?\s+\S", title):
        return int(m.group(1))
    return None


def detect_chapters(
    pages: list[str],
    wanted: dict[int, str],
    outline: list[tuple[str, int]] | None = None,
) -> list[Found]:
    """Propose page ranges for the wanted chapters ({number: title}). `pages[0]` is page 1.

    `outline` is a list of (bookmark title, 1-based page). Chapters that can't be located come back with
    `start=None` so the UI can ask for the pages.
    """
    starts: dict[int, tuple[int, str]] = {}  # chapter number -> (page, source)

    for title, page in outline or []:
        n = _outline_number(title)
        if n is not None and 1 <= page <= len(pages) and n not in starts:
            starts[n] = (page, "bookmark")

    # Headings, taken in book order: each chapter opens on its first heading after the previous chapter's.
    occurrences: dict[int, list[int]] = {}
    for page_number, text in enumerate(pages, 1):
        if is_contents_page(text):
            continue
        for n in chapter_marks(text, set(wanted)):
            occurrences.setdefault(n, []).append(page_number)
    previous = 0
    for n in sorted(occurrences):
        if n in starts:
            previous = max(previous, starts[n][0])
            continue
        after = [p for p in occurrences[n] if p > previous]
        if after:
            starts[n] = (after[0], "heading")
            previous = after[0]

    # Wanted chapters still missing: look for their title near the top of a page.
    for n in sorted(wanted):
        title = _norm(wanted[n])
        if n in starts or len(title) < 6:
            continue
        floor = max((s for m, (s, _) in starts.items() if m < n), default=0)
        ceiling = min((s for m, (s, _) in starts.items() if m > n), default=len(pages) + 1)
        for page_number in range(floor + 1, ceiling):
            text = pages[page_number - 1]
            if not is_contents_page(text) and title in _norm(" ".join(_lines(text, TOP_LINES))):
                starts[n] = (page_number, "title")
                break

    back_matter = [page_number for page_number, text in enumerate(pages, 1) if is_back_matter(text)]
    boundaries = sorted({page for page, _ in starts.values()})

    results = []
    for n in sorted(wanted):
        found = Found(number=n, title=wanted[n])
        if n in starts:
            found.start, found.source = starts[n]
            later = [b for b in boundaries if b > found.start] + [b for b in back_matter if b > found.start]
            if later:
                found.end = min(later) - 1
            else:
                found.end = len(pages)
                found.warnings.append("Couldn't find where this chapter ends; check the last page.")
            found.snippet = " · ".join(_lines(pages[found.start - 1], 3))[:160]
            if found.pages and found.pages > LONG_CHAPTER_PAGES:
                found.warnings.append(f"{found.pages} pages is unusually long; check the last page.")
        else:
            found.warnings.append("Couldn't find this chapter's opening page.")
        results.append(found)
    return results

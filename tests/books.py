"""A small fake textbook for tests: cover, contents, five chapters, glossary and index."""

import io

from conftest import make_pdf
from pypdf import PdfReader, PdfWriter

TITLES = {1: "Introduction", 2: "The Cell", 3: "Energy", 4: "Genes", 5: "Evolution"}
PAGES_PER_CHAPTER = 6
FIRST_CHAPTER_PAGE = 3


def chapter_start(n: int) -> int:
    return FIRST_CHAPTER_PAGE + PAGES_PER_CHAPTER * (n - 1)


def book_pages() -> list[str]:
    pages = ["Biology A Textbook Twelfth Edition Cover Page"]
    toc = ["Contents"] + [f"Chapter {n} {t} {chapter_start(n)}" for n, t in TITLES.items()]
    pages.append("\n".join(toc))
    for n, title in TITLES.items():
        pages.append(f"Chapter {n} {title}\nThis opening page introduces chapter {n} and what you will learn")
        for k in range(2, PAGES_PER_CHAPTER + 1):
            body = f"Running text of chapter {n} page {k} with plenty of ordinary words on it"
            if (n, k) == (1, 3):
                body = (
                    "Chapter 3 is covered later\n" + body
                )  # a cross-reference near the top of an early page
            pages.append(body)
    pages.append("Glossary\nAtom the smallest unit of an element and other useful terms")
    pages.append("Index\nAtoms 4 Cells 9 Energy 15 Genes 21 and many other entries")
    return pages


def book_pdf(*, bookmarks: bool = False, pages: list[str] | None = None) -> bytes:
    data = make_pdf(pages or book_pages())
    if not bookmarks:
        return data
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(data)))
    for n, title in TITLES.items():
        writer.add_outline_item(f"Chapter {n}: {title}", chapter_start(n) - 1)  # 0-based page index
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

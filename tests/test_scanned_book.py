"""The real thing: an image-only PDF, read by real Tesseract, split into chapters by detection.

Skipped where tesseract/poppler aren't installed (CI installs them, and so does the Docker image).
"""

import io
import shutil
import threading

import pytest
from pypdf import PdfReader
from scanned import scanned_pdf

from readcue.books import book_path, process_next_book
from readcue.detect import detect_chapters

pytestmark = pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="tesseract/poppler not installed"
)

BODY = "The quick brown fox jumps over the lazy dog near the river bank"
PAGES = [
    "Introductory Biology Textbook",  # 1 cover
    "Chapter 1 Introduction\n" + BODY,  # 2
    BODY + " page three",  # 3
    BODY + " page four",  # 4
    "Chapter 2 The Cell\n" + BODY,  # 5
    BODY + " page six",  # 6
    BODY + " page seven",  # 7
    "Glossary\nAtom and molecule and many other words",  # 8
]


def test_a_scanned_book_is_ocred_and_its_chapters_are_found(db, cfg, tmp_path):
    pdf = scanned_pdf(PAGES)
    assert all(not (page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(pdf)).pages)

    course = db.add_course("BIO 101")
    book_id = db.add_book(course.id, "scanned.pdf")
    book_path(cfg, book_id).parent.mkdir(parents=True, exist_ok=True)
    book_path(cfg, book_id).write_bytes(pdf)

    assert process_next_book(db, cfg, threading.Event()) is True
    book = db.get_book(book_id)
    assert (book.status, book.page_count, book.scanned_pages) == ("ready", 8, 8), book.error
    pages = db.get_book_page_texts(book_id, 8)
    print("OCR read page 5 as:", repr(pages[4]))

    found = {f.number: f for f in detect_chapters(pages, {1: "Introduction", 2: "The Cell"})}
    assert (found[1].start, found[1].end) == (2, 4), pages
    assert (found[2].start, found[2].end) == (5, 7), pages
    assert "quick brown fox" in "\n".join(pages[5:7]).lower()

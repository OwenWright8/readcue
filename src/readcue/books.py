"""Read a whole textbook in the background: text where a page has it, OCR where it doesn't.

A book can be hundreds of pages of scans, so this runs in its own thread, stores each batch of pages as it goes
(the page list is what chapter detection and splitting read from later), and resumes where it stopped if the app
restarts halfway through.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from . import ocr
from .config import Config
from .db import Database
from .errors import ReadcueError
from .extract import MIN_PAGE_CHARS
from .models import Book

log = logging.getLogger(__name__)

BATCH_PAGES = 8  # pages per step: keeps progress moving and lets other OCR jobs take a turn between batches
OCR_DPI = (
    200  # lower than the 300 used for single chapters: a book has hundreds of pages, and this reads fine
)
MAX_BOOK_PAGES = 2500


def book_path(cfg: Config, book_id: int) -> Path:
    return cfg.data_dir / "books" / f"{book_id}.pdf"


def read_outline(reader: PdfReader) -> list[tuple[str, int]]:
    """The PDF's bookmarks as (title, 1-based page), flattened. Empty if it has none or they're unreadable."""
    found: list[tuple[str, int]] = []

    def walk(nodes) -> None:
        for node in nodes:
            if isinstance(node, list):
                walk(node)
                continue
            try:
                page = reader.get_destination_page_number(node)
                title = str(node.title or "").strip()
            except Exception:
                continue
            if title and page is not None and page >= 0:
                found.append((title, page + 1))

    try:
        walk(reader.outline)
    except Exception:
        return []
    return found


def _page_text(reader: PdfReader, page: int) -> str:
    try:
        return reader.pages[page - 1].extract_text() or ""
    except Exception:  # pypdf can choke on an odd page; treat it as unreadable and let OCR try
        return ""


def process_book(db: Database, cfg: Config, book: Book, stop: threading.Event) -> None:
    path = book_path(cfg, book.id)
    if not path.is_file():
        raise ReadcueError("The uploaded file is missing. Delete this textbook and upload it again.")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ReadcueError("This PDF is password-protected.")
        total = len(reader.pages)
        outline = read_outline(reader)
    except PyPdfError as e:
        raise ReadcueError(f"Couldn't read the PDF: {e}") from e
    if total > MAX_BOOK_PAGES:
        raise ReadcueError(f"This PDF has {total:,} pages; the limit is {MAX_BOOK_PAGES:,}.")

    db.set_book_info(book.id, page_count=total, outline=outline)
    todo = [
        p for p in range(1, total + 1) if p not in db.stored_book_pages(book.id)
    ]  # resume after a restart
    unread = 0
    for i in range(0, len(todo), BATCH_PAGES):
        if stop.is_set():
            return  # stays "reading"; it's re-queued and resumed at the next start
        batch = todo[i : i + BATCH_PAGES]
        texts = {p: _page_text(reader, p) for p in batch}
        scanned = [p for p, text in texts.items() if len(text.strip()) < MIN_PAGE_CHARS]
        if scanned and ocr.available():
            texts.update(ocr.ocr_pdf_file(path, scanned, cfg.ocr_lang, dpi=OCR_DPI))
        elif scanned:
            unread += len(scanned)
        db.save_book_pages(book.id, texts, scanned=len(scanned))

    note = f"{unread} pages had no text and OCR isn't installed, so they were skipped." if unread else ""
    db.finish_book(book.id, note)
    path.unlink(missing_ok=True)  # the page texts are all that's needed from here on
    log.info("Finished reading %s (%d pages)", book.name, total)


def process_next_book(db: Database, cfg: Config, stop: threading.Event) -> bool:
    """Read the oldest queued book. Returns False if there was none."""
    book = db.claim_queued_book()
    if book is None:
        return False
    log.info("Reading textbook %s for %s", book.name, book.course_name)
    try:
        process_book(db, cfg, book, stop)
    except ReadcueError as e:
        log.warning("Couldn't read %s: %s", book.name, e)
        db.fail_book(book.id, str(e))
    except Exception as e:
        log.exception("Unexpected error reading %s", book.name)
        db.fail_book(book.id, f"Unexpected error: {e}")
    return True


def run_books(db: Database, cfg: Config, stop: threading.Event, wake: threading.Event) -> None:
    db.reset_reading_books()
    while not stop.is_set():
        if not process_next_book(db, cfg, stop):
            wake.wait(timeout=10)
            wake.clear()

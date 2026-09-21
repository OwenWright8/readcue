import threading

import pytest
from books import TITLES, book_pages, book_pdf, chapter_start
from conftest import make_pdf

from readcue import books, ocr
from readcue.books import book_path, process_next_book, read_outline
from readcue.detect import detect_chapters


def upload(db, cfg, pdf: bytes, name="bio.pdf"):
    course = db.add_course("BIO 101")
    book_id = db.add_book(course.id, name)
    path = book_path(cfg, book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)
    return course, book_id


def run(db, cfg):
    return process_next_book(db, cfg, threading.Event())


def test_reads_every_page_of_a_text_book_and_keeps_the_pdf_for_later_splits(db, cfg):
    _, book_id = upload(db, cfg, book_pdf())
    assert run(db, cfg) is True
    book = db.get_book(book_id)
    assert (book.status, book.page_count, book.pages_done, book.scanned_pages) == ("ready", 34, 34, 0)
    pages = db.get_book_page_texts(book_id, book.page_count)
    assert "Chapter 2 The Cell" in pages[chapter_start(2) - 1]
    assert book_path(cfg, book_id).is_file()  # kept: chapters are cut from it, and more can be split later
    assert run(db, cfg) is False  # nothing else queued


def test_detection_works_on_what_was_stored(db, cfg):
    _, book_id = upload(db, cfg, book_pdf())
    run(db, cfg)
    pages = db.get_book_page_texts(book_id, db.get_book(book_id).page_count)
    found = {f.number: f for f in detect_chapters(pages, dict(TITLES))}
    assert found[3].start == chapter_start(3) and found[3].end == chapter_start(4) - 1


def test_bookmarks_are_read_and_saved(db, cfg):
    _, book_id = upload(db, cfg, book_pdf(bookmarks=True))
    run(db, cfg)
    outline = db.get_book_outline(book_id)
    assert outline[0] == ("Chapter 1: Introduction", chapter_start(1))
    assert len(outline) == len(TITLES)


def test_read_outline_is_empty_when_there_are_no_bookmarks(tmp_path):
    from pypdf import PdfReader

    path = tmp_path / "plain.pdf"
    path.write_bytes(book_pdf())
    assert read_outline(PdfReader(str(path))) == []


def test_scanned_pages_are_ocred_in_batches_and_text_pages_are_not(db, cfg, monkeypatch):
    pages = book_pages()
    scanned = {4, 5, 20}
    for p in scanned:
        pages[p - 1] = ""  # no text layer
    calls = []

    def fake_ocr(path, page_numbers, lang, *, dpi):
        calls.append((list(page_numbers), lang, dpi))
        return {p: f"OCR text of page {p} from the scan of the book" for p in page_numbers}

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_pdf_file", fake_ocr)
    cfg.ocr_lang = "spa"
    _, book_id = upload(db, cfg, make_pdf(pages))
    run(db, cfg)

    assert sorted(p for call in calls for p in call[0]) == sorted(scanned)
    assert all(lang == "spa" and dpi == books.OCR_DPI for _, lang, dpi in calls)
    book = db.get_book(book_id)
    assert book.scanned_pages == 3 and book.status == "ready"
    stored = db.get_book_page_texts(book_id, book.page_count)
    assert stored[3] == "OCR text of page 4 from the scan of the book"
    assert "Running text" in stored[6]


def test_without_ocr_tools_scanned_pages_are_skipped_with_a_note(db, cfg, monkeypatch):
    pages = book_pages()
    pages[3] = pages[4] = ""
    monkeypatch.setattr(ocr, "available", lambda: False)
    _, book_id = upload(db, cfg, make_pdf(pages))
    run(db, cfg)
    book = db.get_book(book_id)
    assert book.status == "ready" and "2 pages had no text" in book.note


def test_an_interrupted_book_resumes_without_redoing_stored_pages(db, cfg, monkeypatch):
    pages = book_pages()
    for p in (3, 30):
        pages[p - 1] = ""
    _, book_id = upload(db, cfg, make_pdf(pages))
    db.set_book_info(book_id, page_count=34, outline=[])
    db.save_book_pages(
        book_id, {p: f"already read page {p} of the book text" for p in range(1, 21)}, scanned=1
    )
    monkeypatch.setattr(ocr, "available", lambda: True)
    seen = []
    monkeypatch.setattr(
        ocr,
        "ocr_pdf_file",
        lambda path, nums, lang, *, dpi: seen.extend(nums) or {p: f"ocr {p} text here" * 3 for p in nums},
    )
    run(db, cfg)
    assert seen == [30]  # page 3 was already stored, so it wasn't read again
    book = db.get_book(book_id)
    assert book.pages_done == 34 and book.status == "ready"
    assert db.get_book_page_texts(book_id, 34)[2].startswith("already read page 3")


def test_reading_that_was_in_progress_at_shutdown_is_requeued(db, cfg):
    _, book_id = upload(db, cfg, book_pdf())
    assert db.claim_queued_book().id == book_id
    assert db.get_book(book_id).status == "reading"
    db.reset_reading_books()
    assert db.get_book(book_id).status == "queued"


def test_stopping_mid_book_leaves_it_resumable(db, cfg):
    _, book_id = upload(db, cfg, book_pdf())
    stop = threading.Event()
    stop.set()
    process_next_book(db, cfg, stop)
    book = db.get_book(book_id)
    assert book.status == "reading" and book.pages_done == 0
    assert book_path(cfg, book_id).exists()


@pytest.mark.parametrize(
    ("pdf", "message"),
    [(b"%PDF-1.4 this is not really a pdf", "Couldn't read the PDF"), (None, "file is missing")],
)
def test_unreadable_books_fail_with_a_message(db, cfg, pdf, message):
    _, book_id = upload(db, cfg, pdf or b"x")
    if pdf is None:
        book_path(cfg, book_id).unlink()
    run(db, cfg)
    book = db.get_book(book_id)
    assert book.status == "error" and message in book.error


def test_a_book_over_the_page_limit_is_refused(db, cfg, monkeypatch):
    monkeypatch.setattr(books, "MAX_BOOK_PAGES", 10)
    _, book_id = upload(db, cfg, book_pdf())
    run(db, cfg)
    assert "limit" in db.get_book(book_id).error


def test_a_failed_book_can_be_retried(db, cfg):
    _, book_id = upload(db, cfg, book_pdf())
    book_path(cfg, book_id).write_bytes(b"garbage")
    run(db, cfg)
    assert db.get_book(book_id).status == "error"
    book_path(cfg, book_id).write_bytes(book_pdf())
    db.requeue_book(book_id)
    run(db, cfg)
    assert db.get_book(book_id).status == "ready"

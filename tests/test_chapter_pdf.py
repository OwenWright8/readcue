"""Downloading a chapter's original pages as a PDF."""

import io
import threading
from datetime import date

import pytest
from books import TITLES, book_pdf, chapter_start
from conftest import FakeProvider, make_pdf, summary_json
from pypdf import PdfReader

from readcue import pdfs, web
from readcue.books import book_path, process_next_book
from readcue.errors import ReadcueError
from readcue.pdfs import chapter_pdf_path, download_name, merge_pdfs, slice_pdf
from readcue.web import create_app

TODAY = date(2026, 10, 1)


def page_text(pdf_bytes: bytes, index: int) -> str:
    return PdfReader(io.BytesIO(pdf_bytes)).pages[index].extract_text()


def page_count(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


# ---- the PDF helpers -------------------------------------------------------------------


def test_slice_pdf_keeps_exactly_those_pages(tmp_path):
    src = tmp_path / "book.pdf"
    src.write_bytes(book_pdf())
    dest = tmp_path / "out" / "ch2.pdf"
    slice_pdf(src, 9, 14, dest)
    data = dest.read_bytes()
    assert page_count(data) == 6 and "Chapter 2 The Cell" in page_text(data, 0)
    assert "chapter 2 page 6" in page_text(data, 5)
    assert not list(dest.parent.glob("*.part"))


@pytest.mark.parametrize("start,end", [(0, 3), (5, 3), (30, 99)])
def test_slice_pdf_rejects_ranges_outside_the_book(tmp_path, start, end):
    src = tmp_path / "book.pdf"
    src.write_bytes(book_pdf())
    with pytest.raises(ReadcueError, match="don't fit"):
        slice_pdf(src, start, end, tmp_path / "x.pdf")
    assert not (tmp_path / "x.pdf").exists()


def test_merge_pdfs_joins_in_order_and_honours_a_page_range(tmp_path):
    a = make_pdf([f"file A page {i} with enough words to count" for i in range(1, 5)])
    b = make_pdf(["file B page 1 with enough words to count"])
    merge_pdfs([a, b], None, tmp_path / "all.pdf")
    merged = (tmp_path / "all.pdf").read_bytes()
    assert page_count(merged) == 5 and "file B page 1" in page_text(merged, 4)
    merge_pdfs([a], "2-3", tmp_path / "some.pdf")
    some = (tmp_path / "some.pdf").read_bytes()
    assert page_count(some) == 2 and "file A page 2" in page_text(some, 0)


def test_unreadable_pdfs_fail_cleanly(tmp_path):
    with pytest.raises(ReadcueError, match="Couldn't read the PDF"):
        merge_pdfs([b"%PDF-1.4 not really"], None, tmp_path / "x.pdf")


def test_download_names_are_readable_and_filesystem_safe():
    assert download_name("BIO 101", 4, "Cell Structure") == "BIO 101 - Chapter 4 - Cell Structure.pdf"
    assert download_name("BIO 101", 4, "") == "BIO 101 - Chapter 4.pdf"
    assert "/" not in download_name("A/B: C", 1, 'What is "life"?') and download_name(
        "A/B: C", 1, "x"
    ).endswith(".pdf")
    assert len(download_name("x" * 500, 1, "")) <= 160


# ---- uploading a chapter ---------------------------------------------------------------


@pytest.fixture
def client(cfg, db, notifier):
    provider = FakeProvider(lambda user: summary_json())
    return create_app(
        cfg, db, provider_factory=lambda: provider, notifier=notifier, today_fn=lambda: TODAY
    ).test_client()


@pytest.fixture
def course(db):
    course = db.add_course("BIO 101")
    for n, title in TITLES.items():
        db.upsert_reading(course.id, n, date(2026, 10, 10 + n), title)
    return course


def add_chapter(client, course, number, files=None, **extra):
    data = {"course_id": course.id, "number": str(number), **extra}
    if files is not None:
        data["file"] = files
    return client.post("/chapters", data=data, content_type="multipart/form-data", follow_redirects=True)


def chapter_of(db, course, number):
    return db.get_chapter(db.find_reading(course.id, number).chapter_id)


def test_a_pdf_upload_can_be_downloaded_again(client, db, cfg, course):
    pdf = make_pdf(
        ["Chapter four first page with plenty of text", "Chapter four second page with plenty of text"]
    )
    add_chapter(client, course, 4, files=(io.BytesIO(pdf), "ch4.pdf"))
    chapter = chapter_of(db, course, 4)
    assert chapter.has_pdf and chapter_pdf_path(cfg, chapter.id).is_file()

    resp = client.get(f"/chapters/{chapter.id}/pdf")
    assert resp.status_code == 200 and resp.mimetype == "application/pdf"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "BIO 101 - Chapter 4 - Genes.pdf" in resp.headers["Content-Disposition"]
    assert page_count(resp.data) == 2 and "Chapter four second page" in page_text(resp.data, 1)


def test_a_page_range_keeps_only_those_pages(client, db, cfg, course):
    pdf = make_pdf([f"page {i} of the whole book, with plenty of words" for i in range(1, 9)])
    add_chapter(client, course, 4, files=(io.BytesIO(pdf), "book.pdf"), pages="3-5")
    data = client.get(f"/chapters/{chapter_of(db, course, 4).id}/pdf").data
    assert page_count(data) == 3 and "page 3 of the whole book" in page_text(data, 0)


def test_several_pdfs_become_one(client, db, cfg, course):
    files = [
        (io.BytesIO(make_pdf(["part one page one, with plenty of words"])), "part1.pdf"),
        (io.BytesIO(make_pdf(["part two page one, with plenty of words"])), "part2.pdf"),
    ]
    add_chapter(client, course, 4, files=files)
    data = client.get(f"/chapters/{chapter_of(db, course, 4).id}/pdf").data
    assert page_count(data) == 2 and "part two" in page_text(data, 1)


@pytest.mark.parametrize("how", ["pasted", "text file", "pdf plus text file"])
def test_sources_that_arent_pages_have_no_pdf(client, db, cfg, course, how):
    if how == "pasted":
        add_chapter(client, course, 4, text="Some pasted chapter text that is long enough.")
    elif how == "text file":
        add_chapter(client, course, 4, files=(io.BytesIO(b"Plain text of the chapter."), "ch4.txt"))
    else:
        files = [
            (io.BytesIO(make_pdf(["A pdf page with plenty of words in it"])), "a.pdf"),
            (io.BytesIO(b"text"), "b.txt"),
        ]
        add_chapter(client, course, 4, files=files)
    chapter = chapter_of(db, course, 4)
    assert not chapter.has_pdf
    assert client.get(f"/chapters/{chapter.id}/pdf").status_code == 404
    assert b"Chapter PDF" not in client.get(f"/summary/{chapter.id}").data


def test_a_pdf_that_cant_be_kept_doesnt_lose_the_chapter(client, db, cfg, course, monkeypatch):
    def broken(*args):
        raise ReadcueError("disk full")

    monkeypatch.setattr(web, "merge_pdfs", broken)
    add_chapter(
        client, course, 4, files=(io.BytesIO(make_pdf(["A page with plenty of words in it here"])), "ch4.pdf")
    )
    chapter = chapter_of(db, course, 4)
    assert chapter.summary_status == "pending" and not chapter.has_pdf


def test_the_buttons_appear_only_when_there_is_a_pdf(client, db, cfg, course):
    add_chapter(
        client, course, 4, files=(io.BytesIO(make_pdf(["A page with plenty of words in it here"])), "ch4.pdf")
    )
    add_chapter(client, course, 5, text="pasted text without pages behind it")
    chapter = chapter_of(db, course, 4)
    assert f"/chapters/{chapter.id}/pdf".encode() in client.get(f"/summary/{chapter.id}").data
    dashboard = client.get("/").data.decode()
    assert dashboard.count("as a PDF") == 2 * 1  # the row's title= and aria-label= for chapter 4 only
    assert "Download chapter 4 as a PDF" in dashboard and "Download chapter 5 as a PDF" not in dashboard


def test_replacing_or_deleting_a_chapter_replaces_or_removes_its_pdf(client, db, cfg, course):
    add_chapter(
        client, course, 4, files=(io.BytesIO(make_pdf(["First upload page with plenty of words"])), "a.pdf")
    )
    chapter = chapter_of(db, course, 4)
    path = chapter_pdf_path(cfg, chapter.id)
    old = path.read_bytes()

    add_chapter(
        client,
        course,
        4,
        files=(
            io.BytesIO(make_pdf(["Second upload page with plenty of words", "Another page here"])),
            "b.pdf",
        ),
    )
    assert path.read_bytes() != old and page_count(path.read_bytes()) == 2

    add_chapter(client, course, 4, text="now just pasted text instead")
    assert not path.exists() and not chapter_of(db, course, 4).has_pdf

    add_chapter(
        client, course, 4, files=(io.BytesIO(make_pdf(["Third upload page with plenty of words"])), "c.pdf")
    )
    assert path.is_file()
    client.post(f"/chapters/{chapter.id}/delete")
    assert not path.exists()


def test_deleting_a_course_removes_its_chapter_pdfs(client, db, cfg, course):
    add_chapter(
        client, course, 4, files=(io.BytesIO(make_pdf(["A page with plenty of words in it here"])), "a.pdf")
    )
    path = chapter_pdf_path(cfg, chapter_of(db, course, 4).id)
    assert path.is_file()
    client.post(f"/courses/{course.id}/delete")
    assert not path.exists()


def test_unknown_chapters_are_404(client):
    assert client.get("/chapters/999/pdf").status_code == 404


# ---- chapters cut from a textbook ------------------------------------------------------


def read_book(client, db, cfg, course):
    client.post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(book_pdf()), "bio.pdf")},
        content_type="multipart/form-data",
    )
    assert process_next_book(db, cfg, threading.Event())


def split(client, rows):
    data = {"count": str(len(rows))}
    for i, (number, start, end) in enumerate(rows):
        data.update(
            {f"include-{i}": "on", f"number-{i}": str(number), f"start-{i}": str(start), f"end-{i}": str(end)}
        )
    return client.post("/books/1/split", data=data, follow_redirects=True)


def test_chapters_cut_from_a_book_get_their_own_pdf(client, db, cfg, course):
    read_book(client, db, cfg, course)
    split(client, [(2, chapter_start(2), chapter_start(3) - 1), (3, chapter_start(3), chapter_start(4) - 1)])
    for number in (2, 3):
        chapter = chapter_of(db, course, number)
        data = client.get(f"/chapters/{chapter.id}/pdf").data
        assert page_count(data) == 6 and f"Chapter {number} {TITLES[number]}" in page_text(data, 0)


def test_the_edited_page_range_is_what_gets_cut(client, db, cfg, course):
    read_book(client, db, cfg, course)
    split(client, [(2, 10, 12)])
    data = client.get(f"/chapters/{chapter_of(db, course, 2).id}/pdf").data
    assert page_count(data) == 3 and "chapter 2 page 2" in page_text(data, 0)


def test_chapter_pdfs_outlive_the_textbook(client, db, cfg, course):
    read_book(client, db, cfg, course)
    split(client, [(2, 9, 14)])
    client.post("/books/1/delete")
    assert not book_path(cfg, 1).exists()
    assert client.get(f"/chapters/{chapter_of(db, course, 2).id}/pdf").status_code == 200


def test_a_book_read_before_pdfs_were_kept_still_makes_chapters(client, db, cfg, course):
    read_book(client, db, cfg, course)
    book_path(cfg, 1).unlink()  # as for a book read by an earlier version
    split(client, [(2, 9, 14)])
    chapter = chapter_of(db, course, 2)
    assert chapter.summary_status == "pending" and not chapter.has_pdf


def test_the_book_page_mentions_the_kept_pdf(client, db, cfg, course):
    read_book(client, db, cfg, course)
    page = client.get("/books/1").data.decode()
    assert "The original PDF" in page and "MB) is kept" in page
    assert pdfs  # module is importable from the package

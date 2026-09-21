import io
import threading
from datetime import date

import pytest
from books import TITLES, book_pdf, chapter_start
from conftest import FakeProvider, summary_json

from readcue.books import book_path, process_next_book
from readcue.web import create_app

TODAY = date(2026, 10, 1)


@pytest.fixture
def client(cfg, db, notifier):
    provider = FakeProvider(lambda user: summary_json())
    app = create_app(cfg, db, provider_factory=lambda: provider, notifier=notifier, today_fn=lambda: TODAY)
    return app.test_client()


@pytest.fixture
def course(db):
    course = db.add_course("BIO 101")
    for n, title in TITLES.items():
        db.upsert_reading(course.id, n, date(2026, 10, 10 + n), title)
    return course


def upload_book(client, course, pdf=None, name="bio.pdf"):
    return client.post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(pdf if pdf is not None else book_pdf()), name)},
        content_type="multipart/form-data",
    )


def read_book(db, cfg):
    assert process_next_book(db, cfg, threading.Event()) is True


def test_upload_queues_the_book_and_redirects_to_its_page(client, db, cfg, course):
    resp = upload_book(client, course)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/books/1")
    book = db.get_book(1)
    assert (book.status, book.name, book.course_id) == ("queued", "bio.pdf", course.id)
    assert book_path(cfg, 1).is_file()


def test_the_book_page_shows_progress_while_it_is_being_read(client, db, cfg, course):
    upload_book(client, course)
    page = client.get("/books/1").data.decode()
    assert "Waiting to start" in page and 'http-equiv="refresh"' in page
    db.claim_queued_book()
    db.set_book_info(1, page_count=200, outline=[])
    db.save_book_pages(1, {p: f"page {p} of the book" for p in range(1, 51)}, scanned=12)
    page = client.get("/books/1").data.decode()
    assert "Page 50 of 200" in page and "12 scanned pages" in page and "width: 25%" in page


def test_once_read_it_proposes_page_ranges_for_the_scheduled_chapters(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    page = client.get("/books/1").data.decode()
    assert "Found the opening page of <strong>5 of 5</strong>" in page
    for n in TITLES:
        assert f'name="start-{n - 1}" value="{chapter_start(n)}"' in page
    assert f'name="end-1" value="{chapter_start(3) - 1}"' in page  # chapter 2 ends before chapter 3 opens
    assert 'http-equiv="refresh"' not in page
    assert page.count("checked aria-label") == 5


def test_chapters_already_added_are_unchecked_and_marked_as_replaced(client, db, cfg, course):
    db.save_chapter(course.id, 2, "The Cell", "old text", "ch2.pdf")
    upload_book(client, course)
    read_book(db, cfg)
    page = client.get("/books/1").data.decode()
    assert "already added: will be replaced" in page
    assert page.count("checked aria-label") == 4


def split(client, rows, count=None):
    data = {"count": str(count if count is not None else len(rows))}
    for i, (number, start, end) in enumerate(rows):
        data.update(
            {f"include-{i}": "on", f"number-{i}": str(number), f"start-{i}": str(start), f"end-{i}": str(end)}
        )
    return client.post("/books/1/split", data=data, follow_redirects=True)


def test_creating_chapters_slices_exactly_those_pages(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    resp = split(
        client, [(2, chapter_start(2), chapter_start(3) - 1), (3, chapter_start(3), chapter_start(4) - 1)]
    )
    assert b"Created 2 chapters from bio.pdf" in resp.data

    two = db.get_chapter(db.find_reading(course.id, 2).chapter_id, with_text=True)
    assert "Chapter 2 The Cell" in two.text and "chapter 2 page 6" in two.text
    assert "chapter 3" not in two.text.lower().replace("chapter 3 is covered later", "")
    assert two.title == "The Cell" and two.summary_status == "pending"
    reading = db.find_reading(course.id, 2)
    assert reading.chapter_id is not None
    sources = {
        c.number: c for c in (db.get_chapter(db.find_reading(course.id, n).chapter_id) for n in (2, 3))
    }
    assert set(sources) == {2, 3}


def test_the_source_records_the_page_range(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    split(client, [(2, 9, 14)])
    with db._conn() as conn:
        assert conn.execute("SELECT source FROM chapters").fetchone()["source"] == "bio.pdf pp. 9-14"


def test_edited_page_ranges_are_respected(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    split(client, [(2, 10, 12)])
    text = db.get_chapter(db.find_reading(course.id, 2).chapter_id, with_text=True).text
    assert "chapter 2 page 2" in text and "chapter 2 page 4" in text
    assert "Chapter 2 The Cell" not in text and "chapter 2 page 5" not in text


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ((2, 0, 5), "fit a 34-page book"),
        ((2, 9, 999), "fit a 34-page book"),
        ((2, 14, 9), "fit a 34-page book"),
    ],
)
def test_page_ranges_outside_the_book_are_rejected_and_nothing_is_created(
    client, db, cfg, course, row, message
):
    upload_book(client, course)
    read_book(db, cfg)
    resp = split(client, [(3, 15, 20), row])
    assert message.encode() in resp.data
    assert db.find_reading(course.id, 3).chapter_id is None  # the valid row wasn't created either


def test_creating_nothing_is_an_error(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    assert (
        b"Tick at least one chapter"
        in client.post("/books/1/split", data={"count": "0"}, follow_redirects=True).data
    )


def test_created_chapters_go_through_the_normal_summary_queue(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    db.update_course_settings(course.id, "now", True)
    split(client, [(2, 9, 14)])
    assert db.list_readings()[1].stage(TODAY, 3) == "queued"


def test_deleting_the_book_keeps_the_chapters_made_from_it(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    split(client, [(2, 9, 14)])
    resp = client.post("/books/1/delete", follow_redirects=True)
    assert b"Chapters already created from it are kept" in resp.data
    assert db.list_books() == [] and db.find_reading(course.id, 2).chapter_id is not None


def test_a_failed_book_shows_the_error_and_can_be_retried(client, db, cfg, course):
    upload_book(client, course, pdf=b"%PDF-1.4 but not really a pdf")
    read_book(db, cfg)
    page = client.get("/books/1").data.decode()
    assert "Couldn&#39;t read the textbook" in page or "Couldn't read the textbook" in page
    assert "Retry" in page
    client.post("/books/1/retry")
    assert db.get_book(1).status == "queued"


def test_uploads_are_checked_before_anything_is_stored(client, db, cfg, course):
    resp = client.post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(b"<html>nope</html>"), "book.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"doesn&#39;t look like a PDF" in resp.data
    resp = client.post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "book.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"has to be a PDF" in resp.data
    assert db.list_books() == []


def test_no_partial_files_are_left_after_a_rejected_upload(client, db, cfg, course):
    client.post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(b"not a pdf"), "book.pdf")},
        content_type="multipart/form-data",
    )
    assert db.list_books() == []
    assert list((cfg.data_dir / "books").glob("*")) == []


def test_the_upload_limit_is_configurable(client, db, cfg, course):
    cfg.max_upload_mb = 1
    app = create_app(cfg, db, today_fn=lambda: TODAY)
    resp = app.test_client().post(
        f"/courses/{course.id}/books",
        data={"file": (io.BytesIO(b"%PDF-" + b"0" * 2_000_000), "big.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"upload is too large (limit 1 MB)" in resp.data


def test_book_needs_a_schedule_to_look_for(client, db, cfg):
    empty = db.add_course("HIST 210")
    upload_book(client, empty)
    read_book(db, cfg)
    assert b"No chapters on the schedule yet" in client.get("/books/1").data


def test_dashboard_offers_the_textbook_and_links_to_it(client, db, cfg, course):
    assert b"Add textbook" in client.get("/").data
    upload_book(client, course)
    page = client.get("/").data.decode()
    assert "bio.pdf" in page and "reading" in page and 'http-equiv="refresh"' in page
    read_book(db, cfg)
    page = client.get("/").data.decode()
    assert "Split into chapters" in page and "34 pages read" in page and 'http-equiv="refresh"' not in page


def test_unknown_books_are_404(client):
    assert client.get("/books/999").status_code == 404
    assert client.get("/courses/999/books/new").status_code == 404


def test_the_check_endpoint_returns_the_pages_around_a_range(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    body = client.get("/books/1/check?start=9&end=14&number=2").get_json()
    assert [c["page"] for c in body["cards"]] == [8, 9, 14, 15]
    assert body["pages"] == 6 and body["cards"][1]["note"] == "Opens Chapter 2."


def test_the_check_endpoint_rejects_bad_input_with_a_message(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    assert client.get("/books/1/check?start=9").status_code == 400
    assert (
        client.get("/books/1/check?start=x&end=3").get_json()["error"]
        == "Enter both the first and last page."
    )
    resp = client.get("/books/1/check?start=9&end=999")
    assert resp.status_code == 400 and "don't fit a 34-page book" in resp.get_json()["error"]


def test_the_check_endpoint_waits_for_the_book_and_404s_for_unknown_ones(client, db, cfg, course):
    upload_book(client, course)  # queued, not read yet
    assert client.get("/books/1/check?start=1&end=2").status_code == 409
    assert client.get("/books/999/check?start=1&end=2").status_code == 404


def test_each_review_row_has_a_check_pages_button_and_panel(client, db, cfg, course):
    upload_book(client, course)
    read_book(db, cfg)
    page = client.get("/books/1").data.decode()
    assert page.count("data-check ") == 5 and page.count('class="pagecheck"') == 5
    assert 'data-check-url="/books/1/check"' in page

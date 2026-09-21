"""Real-browser tests for the JavaScript: the textbook "Check pages" panel.

Needs Playwright and a Chrome install (CI runners have one); skipped otherwise.
"""

import socket
import threading
import time
from datetime import date, timedelta

import pytest
from books import TITLES, book_pdf
from waitress import create_server

from readcue.books import book_path, process_next_book
from readcue.web import create_app

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def site(cfg, db):
    course = db.add_course("BIO 101")
    for n, title in TITLES.items():
        db.upsert_reading(course.id, n, date(2026, 9, 21) + timedelta(days=7 * n), title)
    book_id = db.add_book(course.id, "bio.pdf")
    book_path(cfg, book_id).parent.mkdir(parents=True, exist_ok=True)
    book_path(cfg, book_id).write_bytes(book_pdf())
    process_next_book(db, cfg, threading.Event())

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = create_server(create_app(cfg, db), host="127.0.0.1", port=port)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"  # daemon thread: it dies with the test process


@pytest.fixture
def page(site):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception as e:  # no Chrome installed here
            pytest.skip(f"Chrome isn't available: {e}")
        tab = browser.new_page(viewport={"width": 1280, "height": 1300})
        tab.goto(f"{site}/books/1")
        yield tab
        browser.close()


def open_panel(page, index):
    rows = page.locator(".split:not(.head)")
    rows.nth(index).locator("[data-check]").click()
    panel = page.locator(".pagecheck").nth(index)
    panel.locator(".pc-card").first.wait_for()
    return rows.nth(index), panel


def test_opening_the_panel_shows_the_pages_around_the_chapter(page):
    _, panel = open_panel(page, 1)
    assert panel.locator(".pc-card").count() == 4
    roles = [c.inner_text() for c in panel.locator(".pc-role").all()]
    assert roles == ["JUST BEFORE THE CHAPTER", "FIRST PAGE", "LAST PAGE", "JUST AFTER THE CHAPTER"]
    assert [c.inner_text() for c in panel.locator(".pc-page").all()] == [
        "page 8",
        "page 9",
        "page 14",
        "page 15",
    ]
    assert panel.locator(".pc-first .pc-note").inner_text() == "Opens Chapter 2."
    assert "Opens Chapter 3" in panel.locator(".pc-after .pc-note").inner_text()
    assert "6 pages" in panel.locator(".pc-head").inner_text()


def test_nudging_the_start_updates_the_box_and_the_warnings(page):
    row, panel = open_panel(page, 1)
    panel.locator("button", has_text="Start a page later").click()
    panel.locator(".pc-before .pc-note").wait_for()
    assert row.locator('input[name^="start-"]').input_value() == "10"
    assert "heading is on this page" in panel.locator(".pc-before .pc-note").inner_text()

    panel.locator(".pc-before button", has_text="Start here instead").click()
    panel.locator(".pc-first .pc-note.ok").wait_for()
    assert row.locator('input[name^="start-"]').input_value() == "9"


def test_ending_early_warns_and_typing_a_number_refreshes_the_panel(page):
    row, panel = open_panel(page, 1)
    panel.locator("button", has_text="End a page earlier").click()
    panel.locator(".pc-after .pc-note.warn").wait_for()
    assert row.locator('input[name^="end-"]').input_value() == "13"
    assert "cut short" in panel.locator(".pc-after .pc-note").inner_text()

    row.locator('input[name^="end-"]').fill("14")  # typing, not clicking
    panel.locator(".pc-after .pc-note.ok").wait_for()


def test_whole_page_view_and_closing(page):
    row, panel = open_panel(page, 0)
    first = panel.locator(".pc-first")
    first.locator("button", has_text="Show whole page").click()
    assert first.locator("pre:not([hidden])").count() == 1
    assert "opening page introduces chapter 1" in first.locator("pre:not([hidden])").inner_text()
    first.locator("button", has_text="Show less").click()

    row.locator("[data-check]").click()
    assert not panel.is_visible()
    assert row.locator("[data-check]").get_attribute("aria-expanded") == "false"


def test_a_bad_range_shows_a_message_instead_of_breaking(page):
    row, panel = open_panel(page, 1)
    row.locator('input[name^="end-"]').fill("999")
    panel.locator(".pc-error").wait_for()
    assert "don't fit a 34-page book" in panel.locator(".pc-error").inner_text()
    row.locator('input[name^="end-"]').fill("")
    panel.locator("text=Enter the first and last page").wait_for()


def test_the_page_runs_without_script_errors_under_the_csp(page):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_panel(page, 2)
    assert errors == []

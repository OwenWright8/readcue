import base64
import io
import json
from datetime import date

import pytest
from conftest import FakeProvider, summary_json

from readcue import ocr
from readcue.scheduler import run_check
from readcue.web import create_app
from readcue.worker import process_next

TODAY = date(2026, 10, 1)  # the app's idea of "today" in these tests
SYLLABUS_REPLY = json.dumps(
    {
        "readings": [
            {"chapter": 4, "title": "Cells", "due_date": "2026-10-18"},
            {"chapter": 5, "title": "Energy", "due_date": "2026-10-25"},
        ]
    }
)


@pytest.fixture
def provider():
    return FakeProvider(lambda user: SYLLABUS_REPLY if "<syllabus>" in user else summary_json())


@pytest.fixture
def client(cfg, db, provider, notifier):
    app = create_app(cfg, db, provider_factory=lambda: provider, notifier=notifier, today_fn=lambda: TODAY)
    return app.test_client()


def upload(client, course_id, number, **extra):
    data = {"course_id": course_id, "number": str(number), **extra}
    return client.post("/chapters", data=data, content_type="multipart/form-data", follow_redirects=True)


def test_full_flow_from_syllabus_to_reminder(client, cfg, db, provider, notifier):
    # create the course, then review the extracted schedule
    resp = client.post("/courses", data={"name": "BIO 101"})
    assert resp.status_code == 302
    course = db.list_courses()[0]

    resp = client.post(f"/courses/{course.id}/syllabus", data={"text": "Ch 4 due 10/18\nCh 5 due 10/25"})
    assert resp.status_code == 200
    assert b'value="2026-10-18"' in resp.data and b"Energy" in resp.data

    # fix chapter 5's date on the review screen, then save
    resp = client.post(
        f"/courses/{course.id}/syllabus/save",
        data={
            "count": "2",
            "include-0": "on",
            "chapter-0": "4",
            "title-0": "Cells",
            "due-0": "2026-10-18",
            "include-1": "on",
            "chapter-1": "5",
            "title-1": "Energy",
            "due-1": "2026-10-26",
        },
    )
    assert resp.status_code == 302
    assert [(r.chapter, r.due) for r in db.list_readings()] == [
        (4, date(2026, 10, 18)),
        (5, date(2026, 10, 26)),
    ]

    # uploading early stores the chapter but doesn't summarize it yet
    resp = upload(client, course.id, 4, file=(io.BytesIO(b"Cells are the unit of life."), "ch4.txt"))
    assert b"will be generated on Thu Oct 15" in resp.data
    assert b"is-waiting" in resp.data and b"Oct 15" in resp.data  # the "summary starts" step on the dashboard
    reading = db.find_reading(course.id, 4)
    assert reading.summary_status == "pending"

    lead = cfg.summarize_days_before
    assert process_next(db, lambda: provider, today=date(2026, 10, 14), lead_days=lead) is False
    assert process_next(db, lambda: provider, today=date(2026, 10, 15), lead_days=lead) is True
    assert process_next(db, lambda: provider, today=date(2026, 10, 15), lead_days=lead) is False
    assert db.find_reading(course.id, 4).summary_ready

    page = client.get(f"/summary/{reading.chapter_id}")
    assert page.status_code == 200
    assert b"Cells are the basic unit of life." in page.data and b"The basic unit of life." in page.data
    md = client.get(f"/summary/{reading.chapter_id}.md")
    assert md.mimetype == "text/markdown" and b"## Definitions" in md.data

    # the day before, the reminder goes out with a link to that page
    results = run_check(db, cfg, notifier, today=date(2026, 10, 17), now=cfg.notify_time)
    assert [r.outcome for r in results] == ["sent"]
    assert notifier.sent[0]["url"] == f"http://readcue.test/summary/{reading.chapter_id}"


def test_summarize_now_overrides_the_wait(client, db, provider):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 18))
    upload(client, course.id, 4, text="some pasted text")
    chapter_id = db.find_reading(course.id, 4).chapter_id

    page = client.get(f"/summary/{chapter_id}")
    assert b"Scheduled for Thu Oct 15" in page.data and b"Summarize now" in page.data
    assert b'http-equiv="refresh"' not in page.data  # nothing is happening, so don't keep reloading

    client.post(f"/chapters/{chapter_id}/resummarize")
    assert b'http-equiv="refresh"' in client.get(f"/summary/{chapter_id}").data  # now queued
    assert process_next(db, lambda: provider, today=TODAY, lead_days=3) is True
    assert db.get_chapter(chapter_id).summary_status == "done"


def test_chapter_inside_the_window_is_queued_immediately(client, db):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 3))
    resp = upload(client, course.id, 4, text="text")
    assert b"The summary is being generated" in resp.data and b"Queued" in resp.data


def test_summarizer_failure_is_recorded_and_retryable(client, db):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 2))
    upload(client, course.id, 4, text="some pasted text")
    process_next(db, lambda: FakeProvider(["nope", "still nope"]), today=TODAY, lead_days=3)
    chapter_id = db.find_reading(course.id, 4).chapter_id
    assert db.get_chapter(chapter_id).summary_status == "error"
    assert b"The summary failed" in client.get(f"/summary/{chapter_id}").data

    client.post(f"/chapters/{chapter_id}/resummarize")
    assert db.get_chapter(chapter_id).summary_status == "pending"


def test_chapter_off_schedule_warns_and_waits_for_a_request(client, db, provider):
    course = db.add_course("BIO 101")
    resp = upload(client, course.id, 9, text="x")
    assert b"isn&#39;t on BIO 101" in resp.data
    chapter = db.get_chapter(1)
    assert process_next(db, lambda: provider, today=TODAY, lead_days=3) is False
    assert b"isn't on the schedule" in client.get(f"/summary/{chapter.id}").data


def test_upload_errors_are_shown_not_raised(client, db):
    course = db.add_course("BIO 101")
    assert b"Choose a file or paste the text" in upload(client, course.id, 4).data
    resp = upload(client, course.id, 4, file=(io.BytesIO(b"x"), "book.epub"))
    assert b"Unsupported file type" in resp.data
    resp = upload(
        client, course.id, 4, file=[(io.BytesIO(b"a"), "a.txt"), (io.BytesIO(b"b"), "b.txt")], pages="1-2"
    )
    assert b"single PDF" in resp.data


def test_several_photos_are_ocred_and_joined_in_natural_filename_order(client, cfg, db, monkeypatch):
    seen_langs = []
    monkeypatch.setattr(ocr, "ocr_image", lambda data, lang: seen_langs.append(lang) or data.decode())
    cfg.ocr_lang = "spa"
    course = db.add_course("BIO 101")
    upload(
        client,
        course.id,
        4,
        file=[
            (io.BytesIO(b"page ten"), "IMG_10.jpg"),
            (io.BytesIO(b"page two"), "IMG_2.jpg"),
            (io.BytesIO(b"page one"), "IMG_1.jpg"),
        ],
    )
    chapter = db.get_chapter(1, with_text=True)
    assert chapter.text == "page one\n\npage two\n\npage ten"
    assert seen_langs == ["spa"] * 3


def test_replacing_a_chapter_requeues_it(client, db, provider):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 2))
    upload(client, course.id, 4, text="v1")
    process_next(db, lambda: provider, today=TODAY, lead_days=3)
    assert db.get_chapter(1).summary_status == "done"
    upload(client, course.id, 4, text="v2")
    assert db.get_chapter(1).summary_status == "pending" and db.get_summary(1) is None


def test_manual_edit_and_delete_of_readings(client, db):
    course = db.add_course("BIO 101")
    client.post(
        f"/courses/{course.id}/readings", data={"chapter": "3", "due": "2026-10-01", "title": "Atoms"}
    )
    reading = db.list_readings()[0]
    client.post(f"/readings/{reading.id}/update", data={"due": "2026-10-02", "title": "Atoms"})
    assert db.get_reading(reading.id).due == date(2026, 10, 2)
    client.post(f"/readings/{reading.id}/delete")
    assert db.list_readings() == []


def test_course_names_with_quotes_render_safely(client, db):
    db.add_course('Bob\'s "Intro" <b>class</b>')
    body = client.get("/").data.decode()
    assert "<b>class</b>" not in body


def test_password_protects_everything_but_healthz(cfg, db, provider, notifier):
    cfg.password = "hunter2"
    client = create_app(cfg, db, provider_factory=lambda: provider, notifier=notifier).test_client()
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    good = {"Authorization": "Basic " + base64.b64encode(b"readcue:hunter2").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"readcue:wrong").decode()}
    assert client.get("/", headers=good).status_code == 200
    assert client.get("/", headers=bad).status_code == 401


def test_cross_site_posts_are_refused(client, db):
    resp = client.post("/courses", data={"name": "X"}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403 and db.list_courses() == []
    resp = client.post("/courses", data={"name": "X"}, headers={"Origin": "http://localhost"})
    assert resp.status_code == 302


def test_settings_page_and_test_buttons(client, notifier, provider, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    page = client.get("/settings")
    assert page.status_code == 200 and b"Available" in page.data and b"claude-sonnet-5" in page.data
    provider.responder = ["OK"]
    assert b"responded: OK" in client.post("/settings/test-llm", follow_redirects=True).data
    client.post("/settings/test-push")
    assert notifier.sent[-1]["title"] == "readcue test"


def test_unknown_ids_are_404(client):
    assert client.get("/summary/999").status_code == 404
    assert client.get("/courses/999/syllabus").status_code == 404

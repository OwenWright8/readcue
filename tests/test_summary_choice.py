"""Summarize now vs schedule, and the "summary complete" notification."""

import io
import json
import sqlite3
from datetime import date

import pytest
from conftest import FakeNotifier, FakeProvider, summary_json

from readcue.db import SCHEMA, Database
from readcue.errors import NotifyError, ReadcueError
from readcue.models import Summary
from readcue.notify import build_summary_ready
from readcue.scheduler import SUMMARY_READY, notify_summary_complete, run_check
from readcue.web import create_app
from readcue.worker import process_next

TODAY = date(2026, 10, 1)
DUE = date(2026, 10, 18)  # 17 days out: well outside the 3-day window
SUMMARY = Summary("Cells overview.", ["a"], [])


def seed(db, *, mode="scheduled", notify=True, reading=True):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, mode, notify)
    if reading:
        db.upsert_reading(course.id, 4, DUE, "Cells")
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
    db.save_summary(chapter_id, SUMMARY, "fake")
    return course, db.get_chapter(chapter_id)


# ---- the choice itself -----------------------------------------------------------------


def test_summarize_now_mode_skips_the_wait(db):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, DUE, "Cells")
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
    provider = FakeProvider([summary_json()])
    assert process_next(db, lambda: provider, today=TODAY, lead_days=3) is False  # scheduled: waits

    db.update_course_settings(course.id, "now", True)
    assert db.list_readings()[0].waiting(TODAY, 3) is False
    assert db.list_readings()[0].stage(TODAY, 3) == "queued"
    assert process_next(db, lambda: provider, today=TODAY, lead_days=3) is True
    assert db.get_chapter(chapter_id).summary_status == "done"


def test_summarize_now_mode_also_covers_chapters_that_are_not_on_the_schedule(db):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", True)
    db.save_chapter(course.id, 9, "Extra", "text", "x.pdf")
    assert process_next(db, lambda: FakeProvider([summary_json()]), today=TODAY, lead_days=3) is True


def test_stage_reports_where_each_chapter_is(db):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 1, DUE)
    db.upsert_reading(course.id, 2, DUE)
    db.save_chapter(course.id, 2, "", "text", "x")
    stages = {r.chapter: r.stage(TODAY, 3) for r in db.list_readings()}
    assert stages == {1: "missing", 2: "waiting"}
    assert db.list_readings()[1].stage(date(2026, 10, 15), 3) == "queued"


def test_update_course_settings_rejects_unknown_modes(db):
    course = db.add_course("BIO 101")
    with pytest.raises(ReadcueError, match="must be one of"):
        db.update_course_settings(course.id, "whenever", True)


def test_courses_default_to_scheduled_with_notifications_on(db):
    course = db.add_course("BIO 101")
    assert (course.summary_mode, course.notify_on_summary) == ("scheduled", True)


def test_database_from_before_course_settings_is_migrated(tmp_path):
    import re

    path = tmp_path / "old.db"
    # The schema as it was before the course settings existed: those columns simply aren't there.
    old = re.sub(
        r",\n    summary_mode .*?include_figures INTEGER NOT NULL DEFAULT 0\n", "\n", SCHEMA, flags=re.S
    )
    assert (
        "include_figures" not in old
        and "notify_on_summary" not in old.split("CREATE TABLE IF NOT EXISTS readings")[0]
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(old)
        conn.execute("INSERT INTO courses (name) VALUES ('BIO 101')")
    course = Database(path).list_courses()[0]
    assert (course.summary_mode, course.notify_on_summary, course.include_figures) == (
        "scheduled",
        True,
        False,
    )


# ---- the web flow ----------------------------------------------------------------------

SYLLABUS_REPLY = json.dumps({"readings": [{"chapter": 4, "title": "Cells", "due_date": "2026-10-18"}]})


@pytest.fixture
def client(cfg, db, notifier):
    provider = FakeProvider(lambda user: SYLLABUS_REPLY if "<syllabus>" in user else summary_json())
    app = create_app(cfg, db, provider_factory=lambda: provider, notifier=notifier, today_fn=lambda: TODAY)
    return app.test_client()


def test_review_screen_offers_the_choice_and_saving_applies_it(client, db):
    course = db.add_course("BIO 101")
    page = client.post(f"/courses/{course.id}/syllabus", data={"text": "Ch 4 due 10/18"}).data
    assert b"When should the summaries be written?" in page
    assert b'name="summary_mode" value="scheduled" checked' in page
    assert b"Notify me on Pushover when a summary is complete" in page

    client.post(
        f"/courses/{course.id}/syllabus/save",
        data={
            "count": "1",
            "include-0": "on",
            "chapter-0": "4",
            "title-0": "Cells",
            "due-0": "2026-10-18",
            "summary_mode": "now",
        },  # notify switch left off
    )
    saved = db.get_course(course.id)
    assert (saved.summary_mode, saved.notify_on_summary) == ("now", False)


def test_review_screen_reflects_the_courses_current_choice(client, db):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", False)
    page = client.post(f"/courses/{course.id}/syllabus", data={"text": "Ch 4 due 10/18"}).data
    assert b'value="now" checked' in page and b'name="notify_on_summary" checked' not in page


def test_course_settings_can_be_changed_later(client, db):
    course = db.add_course("BIO 101")
    resp = client.post(
        f"/courses/{course.id}/settings",
        data={"summary_mode": "now", "notify_on_summary": "on"},
        follow_redirects=True,
    )
    assert b"Summary settings updated for BIO 101" in resp.data
    assert db.get_course(course.id).summary_mode == "now"
    bad = client.post(f"/courses/{course.id}/settings", data={"summary_mode": "nope"}, follow_redirects=True)
    assert b"must be one of" in bad.data


def test_uploading_to_a_summarize_now_course_queues_immediately(client, db):
    course = db.add_course("BIO 101")
    db.update_course_settings(course.id, "now", True)
    db.upsert_reading(course.id, 4, DUE, "Cells")
    resp = client.post(
        "/chapters",
        data={"course_id": course.id, "number": "4", "file": (io.BytesIO(b"text"), "c.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"The summary is being generated" in resp.data
    assert db.list_readings()[0].stage(TODAY, 3) == "queued"


def test_dashboard_shows_the_right_action_for_each_stage(client, db):
    course = db.add_course("BIO 101")
    for number in (1, 2, 3, 4):
        db.upsert_reading(course.id, number, DUE, f"Title {number}")
    db.save_summary(db.save_chapter(course.id, 1, "", "t", "x"), SUMMARY, "fake")  # done
    db.save_chapter(course.id, 2, "", "t", "x")  # waiting
    failed = db.save_chapter(course.id, 3, "", "t", "x")
    db.mark_summary_error(failed, "boom")
    # chapter 4: missing
    page = client.get("/").data.decode()
    assert page.count("Read summary</a>") == 1
    assert (
        page.count("Summarize now</button>") == 1
    )  # (the settings popover has its own "Summarize now" card)
    assert page.count("Retry</button>") == 1
    assert "Add chapter" in page  # chapter 4, plus the course header button
    for stage_class in ("step is-done", "step is-waiting", "step is-error"):
        assert stage_class in page


def test_dashboard_stats_and_empty_state(client, db):
    assert "Let's set up your first course" in client.get("/").data.decode()
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 1, date(2026, 10, 3))  # due within a week, no chapter
    page = client.get("/").data.decode()
    assert "Due this week" in page and "Chapters to add" in page
    assert "Next step: add your chapters" in page


# ---- the completion notification -------------------------------------------------------


def test_completion_ping_is_sent_once_with_a_link(db, cfg, notifier):
    _, chapter = seed(db)
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY) is True
    sent = notifier.sent[0]
    assert sent["title"] == "Summary ready: Ch. 4 — BIO 101"
    assert "It's due in 17 days" in sent["message"] and "Cells overview." in sent["message"]
    assert sent["url"] == f"http://readcue.test/summary/{chapter.id}" and sent["url_title"] == "Open summary"
    assert SUMMARY_READY in db.list_readings()[0].notifications

    assert (
        notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY) is False
    )  # e.g. Regenerate
    assert len(notifier.sent) == 1


def test_completion_ping_respects_the_course_setting_and_pushover_config(db, cfg, notifier):
    _, chapter = seed(db, notify=False)
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY) is False
    _, chapter = seed(db, notify=True)
    assert (
        notify_summary_complete(db, cfg, FakeNotifier(configured=False), chapter, SUMMARY, today=TODAY)
        is False
    )
    assert notifier.sent == []


def test_no_completion_ping_when_the_reminder_is_about_to_say_the_same_thing(db, cfg, notifier):
    _, chapter = seed(db)
    # the day before the due date: the reminder (which says "summary is ready") covers it
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=date(2026, 10, 17)) is False
    assert notifier.sent == []
    # ...and it really does go out
    results = run_check(db, cfg, notifier, today=date(2026, 10, 17), now=cfg.notify_time)
    assert [r.kind for r in results] == ["reminder"]


def test_no_completion_ping_after_the_reminder_already_went_out(db, cfg, notifier):
    _, chapter = seed(db)
    run_check(db, cfg, notifier, today=date(2026, 10, 17), now=cfg.notify_time)
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=date(2026, 10, 17)) is False
    assert len(notifier.sent) == 1


def test_overdue_chapter_still_gets_a_completion_ping(db, cfg, notifier):
    _, chapter = seed(db)
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=date(2026, 10, 20)) is True
    assert "was due 2 days ago" in notifier.sent[0]["message"]


def test_chapter_off_the_schedule_gets_a_ping_without_a_due_date(db, cfg, notifier):
    _, chapter = seed(db, reading=False)
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY) is True
    assert "It's due" not in notifier.sent[0]["message"]


def test_rescheduling_allows_a_fresh_completion_ping(db, cfg, notifier):
    course, chapter = seed(db)
    notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY)
    db.upsert_reading(course.id, 4, date(2026, 11, 1))  # moving the date clears earlier notifications
    assert notify_summary_complete(db, cfg, notifier, chapter, SUMMARY, today=TODAY) is True


def test_summary_ready_message():
    title, message = build_summary_ready(
        course="BIO 101",
        chapter=4,
        title="Cells",
        due=date(2026, 10, 18),
        today=date(2026, 10, 17),
        summary=Summary("Overview.", [], []),
    )
    assert title == "Summary ready: Ch. 4 — BIO 101"
    assert message.startswith("Ch. 4: Cells is summarized. It's due tomorrow (Sun Oct 18).")


# ---- worker hook -----------------------------------------------------------------------


def test_worker_calls_on_complete_and_survives_its_failure(db):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, date(2026, 10, 2))
    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "x")
    seen = []

    def on_complete(chapter, summary):
        seen.append((chapter.id, summary.overview))
        raise NotifyError("pushover is down")

    process_next(
        db, lambda: FakeProvider([summary_json()]), today=TODAY, lead_days=3, on_complete=on_complete
    )
    assert seen == [(chapter_id, "Cells are the basic unit of life.\n\nThey come in two broad types.")]
    assert db.get_chapter(chapter_id).summary_status == "done"  # the summary is kept regardless

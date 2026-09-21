import sqlite3
from datetime import date

from conftest import FakeProvider, summary_json

from readcue.db import SCHEMA, Database
from readcue.worker import process_next

DUE = date(2026, 10, 18)  # with a 3-day window, summaries start on Thu Oct 15


def seed(db, *, due=DUE):
    course = db.add_course("BIO 101")
    if due:
        db.upsert_reading(course.id, 4, due, "Cells")
    chapter_id = db.save_chapter(course.id, 4, "Cells", "Cells are the unit of life.", "ch4.pdf")
    return course, chapter_id


def run(db, day, provider=None):
    provider = provider or FakeProvider([summary_json()])
    return process_next(db, lambda: provider, today=day, lead_days=3)


def test_chapter_waits_until_three_days_before_due(db):
    _, chapter_id = seed(db)
    assert run(db, date(2026, 10, 14)) is False  # 4 days out: too early
    assert db.get_chapter(chapter_id).summary_status == "pending"
    assert run(db, date(2026, 10, 15)) is True  # 3 days out
    assert db.get_chapter(chapter_id).summary_status == "done"


def test_no_model_call_is_made_while_waiting(db):
    seed(db)
    provider = FakeProvider([])
    assert run(db, date(2026, 10, 1), provider) is False
    assert provider.calls == []


def test_overdue_chapters_are_summarized_right_away(db):
    _, chapter_id = seed(db)
    assert run(db, date(2026, 10, 25)) is True
    assert db.get_chapter(chapter_id).summary_status == "done"


def test_eligibility_follows_the_due_date_when_the_schedule_changes(db):
    course, chapter_id = seed(db)
    db.upsert_reading(course.id, 4, date(2026, 10, 30))  # syllabus slipped two weeks
    assert run(db, date(2026, 10, 15)) is False
    db.upsert_reading(course.id, 4, date(2026, 10, 16))  # ...then moved up
    assert run(db, date(2026, 10, 15)) is True


def test_summarize_now_overrides_the_wait(db):
    _, chapter_id = seed(db)
    assert run(db, date(2026, 9, 1)) is False
    db.requeue_chapter(chapter_id)
    assert run(db, date(2026, 9, 1)) is True
    assert db.get_chapter(chapter_id).summary_status == "done"


def test_unscheduled_chapters_are_only_summarized_on_request(db):
    _, chapter_id = seed(db, due=None)
    assert run(db, date(2026, 10, 15)) is False
    db.requeue_chapter(chapter_id)
    assert run(db, date(2026, 10, 15)) is True


def test_replacing_a_chapter_goes_back_to_waiting(db):
    course, chapter_id = seed(db)
    db.requeue_chapter(chapter_id)
    assert run(db, date(2026, 9, 1)) is True
    db.save_chapter(course.id, 4, "Cells", "Revised text.", "ch4-v2.pdf")
    chapter = db.get_chapter(chapter_id)
    assert (chapter.summary_status, chapter.summarize_now) == ("pending", False)
    assert run(db, date(2026, 9, 1)) is False


def test_oldest_eligible_chapter_goes_first(db):
    course, first = seed(db)
    db.upsert_reading(course.id, 5, date(2026, 10, 19))
    second = db.save_chapter(course.id, 5, "Energy", "text", "ch5.pdf")
    provider = FakeProvider(lambda user: summary_json())
    day = date(2026, 10, 16)
    assert run(db, day, provider) and db.get_chapter(first).summary_status == "done"
    assert db.get_chapter(second).summary_status == "pending"
    assert run(db, day, provider) and db.get_chapter(second).summary_status == "done"
    assert run(db, day, provider) is False


def test_reading_knows_whether_its_summary_is_being_held_back(db):
    seed(db)
    reading = db.list_readings()[0]
    assert reading.summarize_on(3) == date(2026, 10, 15)
    assert reading.waiting(date(2026, 10, 14), 3) is True
    assert reading.waiting(date(2026, 10, 15), 3) is False


def test_interrupted_jobs_are_requeued_on_startup(db):
    _, chapter_id = seed(db)
    assert db.claim_pending_chapter(DUE) is not None
    assert db.get_chapter(chapter_id).summary_status == "running"
    db.reset_running()
    assert db.get_chapter(chapter_id).summary_status == "pending"


def test_database_created_before_the_summary_window_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    old_schema = SCHEMA.replace("    summarize_now INTEGER NOT NULL DEFAULT 0,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(old_schema)
        conn.execute("INSERT INTO courses (name) VALUES ('BIO 101')")
        conn.execute("INSERT INTO chapters (course_id, number, text, added_at) VALUES (1, 4, 'x', 'now')")
    db = Database(path)
    assert db.get_chapter(1).summarize_now is False

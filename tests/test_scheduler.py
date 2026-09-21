from datetime import date, time

from conftest import FakeNotifier

from readcue.models import Summary
from readcue.scheduler import run_check

DUE = date(2026, 10, 18)
MORNING = time(9, 0)


def seed(db, *, chapter=True, summarized=True):
    course = db.add_course("BIO 101")
    db.upsert_reading(course.id, 4, DUE, "Cells")
    if chapter:
        chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
        if summarized:
            db.save_summary(chapter_id, Summary("Cells overview.", ["a"], []), "fake")
        return course, chapter_id
    return course, None


def check(db, cfg, notifier, day, now=MORNING, **kw):
    return run_check(db, cfg, notifier, today=day, now=now, **kw)


def test_sends_the_day_before_with_summary_link(db, cfg, notifier):
    _, chapter_id = seed(db)
    results = check(db, cfg, notifier, date(2026, 10, 17))
    assert [r.outcome for r in results] == ["sent"]
    sent = notifier.sent[0]
    assert sent["title"] == "Read Ch. 4 (due tomorrow) — BIO 101"
    assert "Summary is ready." in sent["message"] and "Cells overview." in sent["message"]
    assert sent["url"] == f"http://readcue.test/summary/{chapter_id}"


def test_only_sends_once(db, cfg, notifier):
    seed(db)
    check(db, cfg, notifier, date(2026, 10, 17))
    check(db, cfg, notifier, date(2026, 10, 17))
    check(db, cfg, notifier, DUE)
    assert len(notifier.sent) == 1


def test_window_is_lead_days_through_due_date(db, cfg, notifier):
    seed(db)
    assert check(db, cfg, notifier, date(2026, 10, 16)) == []  # too early
    assert check(db, cfg, notifier, date(2026, 10, 19)) == []  # already past due
    assert len(check(db, cfg, notifier, DUE)) == 1  # a missed day-before still fires on the due date
    assert "due today" in notifier.sent[0]["message"]


def test_longer_lead_time(db, cfg, notifier):
    seed(db)
    cfg.notify_days_before = 3
    assert len(check(db, cfg, notifier, date(2026, 10, 15))) == 1
    assert "in 3 days" in notifier.sent[0]["title"]


def test_waits_for_notify_time_unless_forced(db, cfg, notifier):
    seed(db)
    cfg.notify_time = time(8, 0)
    assert check(db, cfg, notifier, date(2026, 10, 17), now=time(7, 59)) == []
    assert len(check(db, cfg, notifier, date(2026, 10, 17), now=time(7, 59), force=True)) == 1


def test_missing_chapter_gets_one_warning_then_a_summary_ready_followup(db, cfg, notifier):
    course, _ = seed(db, chapter=False)
    day = date(2026, 10, 17)
    check(db, cfg, notifier, day)
    assert "hasn't been added" in notifier.sent[0]["message"]
    assert notifier.sent[0]["url"].endswith(f"/chapters/new?course_id={course.id}&chapter=4")

    check(db, cfg, notifier, day)  # still nothing to add: no repeat
    assert len(notifier.sent) == 1

    chapter_id = db.save_chapter(course.id, 4, "Cells", "text", "ch4.pdf")
    check(db, cfg, notifier, day)  # queued but not summarized: still no repeat
    assert len(notifier.sent) == 1

    db.save_summary(chapter_id, Summary("Now ready.", [], []), "fake")
    check(db, cfg, notifier, day)
    assert len(notifier.sent) == 2 and "Summary is ready." in notifier.sent[1]["message"]
    check(db, cfg, notifier, day)
    assert len(notifier.sent) == 2


def test_chapter_added_but_still_summarizing(db, cfg, notifier):
    seed(db, summarized=False)
    check(db, cfg, notifier, date(2026, 10, 17))
    assert "queued or failed" in notifier.sent[0]["message"]


def test_moving_the_due_date_rearms_the_reminder(db, cfg, notifier):
    course, _ = seed(db)
    check(db, cfg, notifier, date(2026, 10, 17))
    db.upsert_reading(course.id, 4, date(2026, 10, 25))
    check(db, cfg, notifier, date(2026, 10, 24))
    assert len(notifier.sent) == 2


def test_failed_send_is_retried_next_tick(db, cfg):
    seed(db)
    notifier = FakeNotifier()
    notifier.fail = True
    results = check(db, cfg, notifier, date(2026, 10, 17))
    assert [r.outcome for r in results] == ["error"]
    notifier.fail = False
    assert [r.outcome for r in check(db, cfg, notifier, date(2026, 10, 17))] == ["sent"]


def test_dry_run_neither_sends_nor_records(db, cfg, notifier):
    seed(db)
    results = check(db, cfg, notifier, date(2026, 10, 17), dry_run=True)
    assert [r.outcome for r in results] == ["would_send"]
    assert notifier.sent == []
    assert len(check(db, cfg, notifier, date(2026, 10, 17))) == 1

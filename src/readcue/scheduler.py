"""Decide which reading reminders are due, and the background loop that sends them."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Config
from .db import Database
from .errors import NotifyError
from .models import Chapter, Reading, Summary
from .notify import PushoverNotifier, build_reminder, build_summary_ready

log = logging.getLogger(__name__)

REMINDER = "reminder"  # sent with the summary ready
REMINDER_NO_SUMMARY = "reminder_no_summary"  # due soon but no summary yet
SUMMARY_READY = "summary_ready"  # a summary finished (sent when the course has notifications on)


@dataclass
class CheckResult:
    reading: Reading
    kind: str
    outcome: str  # sent | would_send | error
    detail: str = ""

    def describe(self) -> str:
        r = self.reading
        text = f"{r.course_name} ch. {r.chapter} ({self.kind}): {self.outcome}"
        return f"{text} - {self.detail}" if self.detail else text


def _link(cfg: Config, reading: Reading, summary: Summary | None) -> tuple[str | None, str]:
    if summary:
        return cfg.link(f"/summary/{reading.chapter_id}"), "Open summary"
    if reading.chapter_id is None:
        return cfg.link(
            f"/chapters/new?course_id={reading.course_id}&chapter={reading.chapter}"
        ), "Add chapter"
    return cfg.link("/"), "Open readcue"


def notify_summary_complete(
    db: Database, cfg: Config, notifier: PushoverNotifier, chapter: Chapter, summary: Summary, *, today: date
) -> bool:
    """Ping the user that a summary finished. Returns True if a notification was sent.

    Skipped when the course has notifications off, and when the user has already been told about
    this chapter's summary: by an earlier completion ping, by its reminder, or because the reminder
    is about to go out (it says the summary is ready, so a second ping would just be noise).
    """
    course = db.get_course(chapter.course_id)
    if not course.notify_on_summary or not notifier.configured:
        return False
    reading = db.find_reading(chapter.course_id, chapter.number)
    if reading is not None:
        window_open = reading.due - timedelta(days=cfg.notify_days_before) <= today <= reading.due
        if SUMMARY_READY in reading.notifications or reading.reminded or window_open:
            return False
    title, message = build_summary_ready(
        course=course.name,
        chapter=chapter.number,
        title=chapter.title or (reading.title if reading else ""),
        due=reading.due if reading else None,
        today=today,
        summary=summary,
    )
    notifier.send(title, message, url=cfg.link(f"/summary/{chapter.id}"), url_title="Open summary")
    if reading is not None:
        db.record_notification(reading.id, SUMMARY_READY)
    return True


def run_check(
    db: Database,
    cfg: Config,
    notifier: PushoverNotifier,
    *,
    today: date,
    now: time,
    dry_run: bool = False,
    force: bool = False,
) -> list[CheckResult]:
    """Send a reminder for every reading due within `notify_days_before` days that hasn't had one.

    A reading gets at most one 'no summary yet' reminder and one 'summary ready' reminder, so a
    chapter uploaded after the first ping still produces a follow-up. Readings already past due are
    left alone, so importing a syllabus mid-semester doesn't fire a burst of stale reminders.
    """
    if not force and now < cfg.notify_time:
        return []

    results: list[CheckResult] = []
    window = timedelta(days=cfg.notify_days_before)
    for reading in db.list_readings():
        if not reading.due - window <= today <= reading.due or reading.reminded:
            continue
        summary = db.get_summary(reading.chapter_id) if reading.summary_ready else None
        if summary is None and REMINDER_NO_SUMMARY in reading.notifications:
            continue

        kind = REMINDER if summary else REMINDER_NO_SUMMARY
        title, message = build_reminder(
            course=reading.course_name,
            chapter=reading.chapter,
            title=reading.title,
            due=reading.due,
            today=today,
            summary=summary,
            has_chapter=reading.chapter_id is not None,
        )
        if dry_run:
            results.append(CheckResult(reading, kind, "would_send", f"{title!r}"))
            continue
        url, url_title = _link(cfg, reading, summary)
        try:
            notifier.send(title, message, url=url, url_title=url_title)
        except NotifyError as e:
            results.append(CheckResult(reading, kind, "error", str(e)))
            continue
        db.record_notification(reading.id, kind)
        results.append(CheckResult(reading, kind, "sent"))
    return results


def run_scheduler(db: Database, cfg: Config, notifier: PushoverNotifier, stop: threading.Event) -> None:
    """Background loop: run_check every `check_interval_seconds` until `stop` is set."""
    logged_errors: set[str] = set()
    while True:
        try:
            results = run_check(db, cfg, notifier, today=date.today(), now=datetime.now().time())
            errors = set()
            for result in results:
                if result.outcome == "error":
                    errors.add(result.describe())
                    if result.describe() not in logged_errors:  # don't repeat the same error every tick
                        log.warning(result.describe())
                else:
                    log.info(result.describe())
            logged_errors = errors
        except Exception:
            log.exception("Reminder check failed")
        if stop.wait(cfg.check_interval_seconds):
            return

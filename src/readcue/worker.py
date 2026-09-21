"""Background worker that summarizes queued chapters one at a time."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, timedelta

from .db import Database
from .errors import NotFoundError, ReadcueError
from .llm.base import LLMProvider
from .models import Chapter, Summary
from .summarize import summarize_chapter

log = logging.getLogger(__name__)


def process_next(
    db: Database,
    provider_factory: Callable[[], LLMProvider],
    *,
    today: date,
    lead_days: int,
    on_complete: Callable[[Chapter, Summary], object] | None = None,
) -> bool:
    """Summarize the oldest chapter that is due within `lead_days` days (or that the user asked for).

    Chapters are held back until then so a syllabus change never wastes a summary, and so the
    work isn't all done at once. Returns False if nothing was eligible.
    """
    chapter = db.claim_pending_chapter(today + timedelta(days=lead_days))
    if chapter is None:
        return False
    log.info("Summarizing %s ch. %s", chapter.course_name, chapter.number)
    try:
        provider = provider_factory()
        summary = summarize_chapter(
            provider, chapter.text, course=chapter.course_name, number=chapter.number, title=chapter.title
        )
        try:
            if db.get_chapter(chapter.id).summary_status != "running":
                log.info(
                    "Chapter %s changed while it was being summarized; discarding the result", chapter.id
                )
                return True
        except NotFoundError:
            return True  # deleted mid-run
        db.save_summary(chapter.id, summary, provider.label)
        log.info("Summary ready for %s ch. %s", chapter.course_name, chapter.number)
        if on_complete is not None:
            try:
                on_complete(chapter, summary)
            except ReadcueError as e:  # e.g. Pushover is down; the summary itself is fine
                log.warning("Couldn't send the summary-complete notification: %s", e)
            except Exception:
                log.exception("Summary-complete notification failed")
    except ReadcueError as e:
        log.warning("Summary failed for %s ch. %s: %s", chapter.course_name, chapter.number, e)
        db.mark_summary_error(chapter.id, str(e))
    except Exception as e:
        log.exception("Unexpected error summarizing chapter %s", chapter.id)
        db.mark_summary_error(chapter.id, f"Unexpected error: {e}")
    return True


def run_worker(
    db: Database,
    provider_factory: Callable[[], LLMProvider],
    stop: threading.Event,
    wake: threading.Event,
    lead_days: int,
    on_complete: Callable[[Chapter, Summary], object] | None = None,
) -> None:
    db.reset_running()
    while not stop.is_set():
        if not process_next(
            db, provider_factory, today=date.today(), lead_days=lead_days, on_complete=on_complete
        ):
            wake.wait(timeout=10)
            wake.clear()

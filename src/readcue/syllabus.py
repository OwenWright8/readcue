"""Extract the chapter reading schedule from syllabus text with the configured LLM."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from . import prompts
from .errors import ReadcueError
from .llm.base import LLMProvider, complete_structured
from .schemas import SYLLABUS_SCHEMA

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduleItem:
    chapter: int
    title: str
    due: date


def _parse_items(data: dict) -> list[ScheduleItem]:
    readings = data.get("readings")
    if not isinstance(readings, list):
        raise ValueError('expected a "readings" array')
    items: dict[int, ScheduleItem] = {}
    for entry in readings:
        try:
            chapter = int(entry["chapter"])
            due = date.fromisoformat(str(entry["due_date"]).strip())
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping unusable syllabus entry: %r", entry)
            continue
        if chapter > 0 and chapter not in items:
            items[chapter] = ScheduleItem(chapter, str(entry.get("title") or "").strip(), due)
    return sorted(items.values(), key=lambda item: (item.due, item.chapter))


def extract_schedule(provider: LLMProvider, text: str, *, today: date) -> list[ScheduleItem]:
    if len(text) > provider.max_input_chars:
        raise ReadcueError(
            f"The syllabus is too long for {provider.label} to read in one go "
            f"({len(text):,} characters). Paste just the schedule section instead."
        )
    return complete_structured(
        provider,
        prompts.SYLLABUS_SYSTEM,
        prompts.syllabus_prompt(text, today.isoformat()),
        _parse_items,
        schema=SYLLABUS_SCHEMA,
    )

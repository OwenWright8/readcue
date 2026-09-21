"""Chapter summarization. Chapters that fit the model's context go in one request; longer ones are
summarized in parts and then combined (definitions are merged in code so none get dropped)."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator

from . import prompts
from .errors import LLMError
from .llm.base import LLMProvider, complete_structured
from .models import Definition, Summary
from .schemas import COMBINE_SCHEMA, SUMMARY_SCHEMA

log = logging.getLogger(__name__)


def _hard_split(text: str, limit: int) -> Iterator[str]:
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text


def split_text(text: str, limit: int) -> list[str]:
    """Split on paragraph boundaries into chunks of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in re.split(r"\n\s*\n", text):
        for piece in _hard_split(paragraph, limit):
            if current and size + len(piece) + 2 > limit:
                chunks.append("\n\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _parse_summary(data: dict) -> Summary:
    summary = Summary.from_dict(data)
    if not summary.overview:
        raise ValueError('the "overview" field is empty')
    return summary


def merge_definitions(parts: list[Summary]) -> list[Definition]:
    merged: dict[str, Definition] = {}
    for part in parts:
        for d in part.definitions:
            key = d.term.lower()
            if key not in merged or len(d.definition) > len(merged[key].definition):
                merged[key] = d
    return list(merged.values())


def summarize_chapter(
    provider: LLMProvider,
    text: str,
    *,
    course: str,
    number: int,
    title: str,
    progress: Callable[[str], None] = log.info,
) -> Summary:
    system = prompts.SUMMARY_SYSTEM
    if len(text) <= provider.max_input_chars:
        return complete_structured(
            provider,
            system,
            prompts.summary_prompt(course, number, title, text),
            _parse_summary,
            schema=SUMMARY_SCHEMA,
        )

    chunks = split_text(text, provider.max_input_chars)
    parts: list[Summary] = []
    for i, chunk in enumerate(chunks, 1):
        progress(f"Summarizing part {i} of {len(chunks)}")
        prompt = prompts.part_prompt(course, number, title, i, len(chunks), chunk)
        parts.append(complete_structured(provider, system, prompt, _parse_summary, schema=SUMMARY_SCHEMA))

    combined = _combine(provider, parts, course=course, number=number, title=title, progress=progress)
    combined.definitions = merge_definitions(parts)
    return combined


def _combine(
    provider: LLMProvider,
    parts: list[Summary],
    *,
    course: str,
    number: int,
    title: str,
    progress: Callable[[str], None],
) -> Summary:
    """Fold part summaries into one, in several rounds if they don't all fit in a single request."""

    def payload(items: list[Summary]) -> str:
        return json.dumps(
            [{"part": i, "overview": p.overview, "key_points": p.key_points} for i, p in enumerate(items, 1)],
            ensure_ascii=False,
        )

    def combine(items: list[Summary]) -> Summary:
        prompt = prompts.combine_prompt(course, number, title, payload(items))
        return complete_structured(
            provider, prompts.SUMMARY_SYSTEM, prompt, _parse_summary, schema=COMBINE_SCHEMA
        )

    while True:
        if len(payload(parts)) <= provider.max_input_chars:
            progress("Combining parts")
            return combine(parts)
        groups: list[list[Summary]] = [[]]
        for part in parts:
            if groups[-1] and len(payload(groups[-1] + [part])) > provider.max_input_chars:
                groups.append([])
            groups[-1].append(part)
        if len(groups) == len(parts):  # nothing can be merged, so the loop would never shrink
            raise LLMError(
                "This chapter is too long for the configured model to summarize. Add it in smaller pieces."
            )
        progress(f"Combining {len(parts)} parts in {len(groups)} groups")
        parts = [group[0] if len(group) == 1 else combine(group) for group in groups]

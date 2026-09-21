from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Protocol, TypeVar

from ..errors import LLMError

log = logging.getLogger(__name__)

T = TypeVar("T")


class LLMProvider(Protocol):
    name: str
    model: str
    max_input_chars: int  # how much source text fits in one request, leaving room for the reply

    @property
    def label(self) -> str: ...

    def complete(self, system: str, user: str, *, json_mode: bool = False, schema: dict | None = None) -> str:
        """One reply. With a `schema`, providers that support it constrain the reply to that JSON shape."""


def parse_json_object(raw: str) -> dict:
    """Pull a JSON object out of a model reply, tolerating code fences and stray prose."""
    text = re.sub(r"^\s*```(?:json)?\s*", "", raw.strip(), flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text, strict=False)  # strict=False tolerates raw newlines/tabs inside strings
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in the reply") from None
        try:
            data = json.loads(text[start : end + 1], strict=False)
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _near_error(raw: str, error: ValueError) -> str:
    """The stretch of a bad reply around the position json.loads complained about."""
    match = re.search(r"char (\d+)", str(error))
    if match:
        position = int(match.group(1))
        return raw[max(0, position - 80) : position + 80]
    return raw[:160]


def complete_structured(
    provider: LLMProvider,
    system: str,
    user: str,
    parse: Callable[[dict], T],
    *,
    schema: dict | None = None,
    attempts: int = 2,
) -> T:
    """Ask for JSON (constrained to `schema` where the provider supports that) and run it through `parse`,
    which raises ValueError if the content is unusable. Retries once, and logs where a bad reply went wrong."""
    prompt, last_error = user, None
    for attempt in range(1, attempts + 1):
        raw = provider.complete(system, prompt, json_mode=True, schema=schema)
        try:
            return parse(parse_json_object(raw))
        except ValueError as e:
            last_error = e
            log.warning(
                "%s sent unusable JSON (attempt %d of %d, %d characters): %s. Near the problem: %r",
                provider.label,
                attempt,
                attempts,
                len(raw),
                e,
                _near_error(raw, e),
            )
            prompt = f"{user}\n\nYour previous reply was rejected ({e}). Reply with only the JSON object."
    raise LLMError(f"{provider.label} didn't return usable JSON: {last_error}")

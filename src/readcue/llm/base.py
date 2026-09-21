from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Protocol, TypeVar

from ..errors import LLMError

T = TypeVar("T")


class LLMProvider(Protocol):
    name: str
    model: str
    max_input_chars: int  # how much source text fits in one request, leaving room for the reply

    @property
    def label(self) -> str: ...

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


def parse_json_object(raw: str) -> dict:
    """Pull a JSON object out of a model reply, tolerating code fences and stray prose."""
    text = re.sub(r"^\s*```(?:json)?\s*", "", raw.strip(), flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in the reply") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def complete_structured(
    provider: LLMProvider, system: str, user: str, parse: Callable[[dict], T], *, attempts: int = 2
) -> T:
    """Ask for JSON and run it through `parse` (which raises ValueError if unusable), retrying once."""
    prompt, last_error = user, None
    for _ in range(attempts):
        raw = provider.complete(system, prompt, json_mode=True)
        try:
            return parse(parse_json_object(raw))
        except ValueError as e:
            last_error = e
            prompt = f"{user}\n\nYour previous reply was rejected ({e}). Reply with only the JSON object."
    raise LLMError(f"{provider.label} didn't return usable JSON: {last_error}")

"""JSON schemas for the structured replies we ask models for.

With Claude these go to `output_config.format`, which constrains decoding so the reply is always valid JSON
of exactly this shape. (Free-form "please reply in JSON" is not reliable: a definition containing a quoted
term is enough to produce an unescaped quote and a reply that can't be parsed.) Ollama accepts the same
schemas as its `format`. Claude requires every object to list all its properties as `required` and set
`additionalProperties: false`.
"""

from __future__ import annotations

_STRING = {"type": "string"}
_NUMBER = {"type": "number"}
_STRINGS = {"type": "array", "items": _STRING}


def _object(**properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


SUMMARY_SCHEMA = _object(
    overview=_STRING,
    key_points=_STRINGS,
    definitions={"type": "array", "items": _object(term=_STRING, definition=_STRING)},
)

# Combining part-summaries: definitions are merged in code, so the model only writes these two.
COMBINE_SCHEMA = _object(overview=_STRING, key_points=_STRINGS)

SYLLABUS_SCHEMA = _object(
    readings={
        "type": "array",
        "items": _object(chapter={"type": "integer"}, title=_STRING, due_date=_STRING),
    }
)

# Figures worth clipping. Box coordinates are fractions (0 to 1) of the page image's width and height, measured
# from its top left corner.
FIGURES_SCHEMA = _object(
    figures={
        "type": "array",
        "items": _object(
            page={"type": "integer"},
            x0=_NUMBER,
            y0=_NUMBER,
            x1=_NUMBER,
            y1=_NUMBER,
            caption=_STRING,
            why=_STRING,
            importance={"type": "integer"},
        ),
    }
)

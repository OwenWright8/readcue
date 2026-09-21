import json
from datetime import date

import pytest
from conftest import FakeProvider

from readcue.errors import ReadcueError
from readcue.syllabus import extract_schedule

TODAY = date(2026, 9, 1)


def test_extracts_sorts_and_dedupes():
    reply = json.dumps(
        {
            "readings": [
                {"chapter": 5, "title": "Energy", "due_date": "2026-10-25"},
                {"chapter": "4", "title": "Cells", "due_date": "2026-10-18"},
                {"chapter": 4, "title": "Duplicate", "due_date": "2026-11-01"},
                {"chapter": 6, "title": "Bad date", "due_date": "October"},
                {"title": "No chapter", "due_date": "2026-10-01"},
            ]
        }
    )
    items = extract_schedule(FakeProvider([reply]), "syllabus", today=TODAY)
    assert [(i.chapter, i.title, i.due) for i in items] == [
        (4, "Cells", date(2026, 10, 18)),
        (5, "Energy", date(2026, 10, 25)),
    ]


def test_prompt_includes_today_and_text():
    provider = FakeProvider([json.dumps({"readings": []})])
    assert extract_schedule(provider, "READ CH 4 BY 10/18", today=TODAY) == []
    assert "2026-09-01" in provider.calls[0] and "READ CH 4 BY 10/18" in provider.calls[0]


def test_rejects_syllabus_too_long_for_the_model():
    with pytest.raises(ReadcueError, match="too long"):
        extract_schedule(FakeProvider([], max_input_chars=10), "x" * 11, today=TODAY)

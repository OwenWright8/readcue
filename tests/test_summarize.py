import json

import pytest
from conftest import FakeProvider, summary_json

from readcue.errors import LLMError
from readcue.llm.base import parse_json_object
from readcue.summarize import split_text, summarize_chapter


def run(provider, text="Some chapter text."):
    return summarize_chapter(provider, text, course="BIO 101", number=4, title="Cells")


def test_parse_json_object_handles_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! Here you go: {"a": 1} Hope that helps.') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_single_pass_summary():
    provider = FakeProvider([summary_json()])
    summary = run(provider)
    assert summary.overview.startswith("Cells are the basic unit")
    assert summary.key_points == ["Cells are basic", "Two cell types"]
    assert [d.term for d in summary.definitions] == ["Cell"]
    assert len(provider.calls) == 1
    assert "<chapter>" in provider.calls[0] and "Chapter 4: Cells" in provider.calls[0]


def test_retries_once_on_bad_json():
    provider = FakeProvider(["not json at all", summary_json()])
    assert run(provider).overview
    assert len(provider.calls) == 2
    assert "rejected" in provider.calls[1]


def test_gives_up_after_second_bad_reply():
    with pytest.raises(LLMError, match="usable JSON"):
        run(FakeProvider(["nope", summary_json(overview="")]))


def test_tolerates_definitions_as_a_mapping():
    provider = FakeProvider([summary_json(definitions={"Mitosis": "Cell division."})])
    assert run(provider).definitions[0].term == "Mitosis"


def test_long_chapter_is_summarized_in_parts_and_definitions_merged():
    paragraphs = [f"Paragraph {i} " + "word " * 30 for i in range(6)]
    text = "\n\n".join(paragraphs)

    def responder(user):
        if "<chapter_part>" in user:
            part = int(user.split("This is part ")[1].split(" of")[0])
            defs = [{"term": "Cell", "definition": "short" if part == 1 else "a much longer definition"}]
            defs.append({"term": f"Term{part}", "definition": "x"})
            return summary_json(overview="short overview", key_points=["k"], definitions=defs)
        assert "<parts>" in user
        return json.dumps({"overview": "Combined overview.", "key_points": ["combined"]})

    provider = FakeProvider(responder, max_input_chars=400)
    summary = run(provider, text)
    chunks = split_text(text, 400)
    assert len(chunks) > 1
    assert len(provider.calls) == len(chunks) + 1  # one per part, plus the combine step
    assert summary.overview == "Combined overview."
    terms = {d.term: d.definition for d in summary.definitions}
    assert terms["Cell"] == "a much longer definition"  # duplicates keep the fuller definition
    assert {f"Term{i}" for i in range(1, len(chunks) + 1)} <= set(terms)


def test_parts_too_big_to_combine_at_once_are_combined_in_rounds():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(12))
    combine_sizes = []

    def responder(user):
        if "<chapter_part>" in user:
            return summary_json(overview="p" * 120, key_points=["k" * 20])
        combine_sizes.append(len(user))
        return json.dumps({"overview": "o" * 120, "key_points": ["merged"]})

    provider = FakeProvider(responder, max_input_chars=400)
    summary = run(provider, text)
    assert summary.overview == "o" * 120
    assert len(combine_sizes) > 1  # needed more than one combine call to get down to one summary


def test_gives_up_when_a_single_part_summary_cannot_fit():
    provider = FakeProvider(lambda user: summary_json(overview="p" * 500), max_input_chars=400)
    with pytest.raises(LLMError, match="too long"):
        run(provider, "\n\n".join("word " * 60 for _ in range(4)))


def test_split_text_respects_limit_and_never_loses_text():
    text = "\n\n".join("para " + "x" * 90 for _ in range(10)) + "\n\n" + "y" * 500
    chunks = split_text(text, 200)
    assert all(0 < len(c) <= 200 for c in chunks)
    assert "".join(c.replace("\n", "").replace(" ", "") for c in chunks) == text.replace("\n", "").replace(
        " ", ""
    )


def test_short_text_is_one_chunk():
    assert split_text("hello", 100) == ["hello"]

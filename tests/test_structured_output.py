"""Replies are constrained to a JSON schema, so a stray quote in a definition can't break a summary."""

import json
import logging
from datetime import date

import anthropic
import httpx2
import pytest
from conftest import FakeProvider, summary_json

from readcue import schemas
from readcue.config import Config
from readcue.errors import LLMError
from readcue.llm.anthropic_provider import AnthropicProvider
from readcue.llm.base import complete_structured, parse_json_object
from readcue.llm.ollama import OllamaProvider
from readcue.summarize import summarize_chapter
from readcue.syllabus import extract_schedule

ALL_SCHEMAS = {
    "summary": schemas.SUMMARY_SCHEMA,
    "combine": schemas.COMBINE_SCHEMA,
    "syllabus": schemas.SYLLABUS_SCHEMA,
}


# ---- the schemas obey Claude's strict-mode rules ---------------------------------------


def objects_in(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from objects_in(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from objects_in(value)


@pytest.mark.parametrize("name", ALL_SCHEMAS)
def test_every_object_lists_all_properties_and_forbids_extras(name):
    found = list(objects_in(ALL_SCHEMAS[name]))
    assert found
    for obj in found:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_schemas_survive_a_json_round_trip():
    for schema in ALL_SCHEMAS.values():
        assert json.loads(json.dumps(schema)) == schema


# ---- Claude: the request carries the schema --------------------------------------------


def claude(handler) -> AnthropicProvider:
    provider = AnthropicProvider(Config(provider="anthropic", anthropic_api_key="test-key"))
    provider._client = anthropic.Anthropic(
        api_key="test-key", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    return provider


def message(text: str, stop_reason: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )  # fmt: skip


def bad_request(text: str) -> httpx2.Response:
    return httpx2.Response(
        400, json={"type": "error", "error": {"type": "invalid_request_error", "message": text}}
    )


def test_schema_is_sent_as_output_config_format():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return message('{"overview": "x", "key_points": [], "definitions": []}')

    reply = claude(handler).complete("sys", "user", json_mode=True, schema=schemas.SUMMARY_SCHEMA)
    assert json.loads(reply)["overview"] == "x"
    assert seen[0]["output_config"] == {"format": {"type": "json_schema", "schema": schemas.SUMMARY_SCHEMA}}
    assert seen[0]["model"] == "claude-sonnet-5"


def test_no_schema_means_no_output_config():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return message("OK")

    assert claude(handler).complete("sys", "Reply with OK") == "OK"
    assert "output_config" not in seen[0]


def test_falls_back_to_plain_json_when_claude_rejects_the_schema(caplog):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("output_config" in body)
        return (
            bad_request("output_config.format is not supported") if "output_config" in body else message("{}")
        )

    with caplog.at_level(logging.WARNING):
        assert claude(handler).complete("s", "u", json_mode=True, schema=schemas.COMBINE_SCHEMA) == "{}"
    assert seen == [True, False]
    assert "rejected structured outputs" in caplog.text


def test_a_400_without_a_schema_is_reported_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return bad_request("prompt is too long")

    with pytest.raises(LLMError, match=r"\(400\): prompt is too long"):
        claude(handler).complete("s", "u")
    assert len(calls) == 1


def test_a_400_that_persists_without_the_schema_still_surfaces():
    with pytest.raises(LLMError, match="prompt is too long"):
        claude(lambda request: bad_request("prompt is too long")).complete(
            "s", "u", schema=schemas.SUMMARY_SCHEMA
        )


def test_claude_stop_reasons_are_still_handled_with_a_schema():
    with pytest.raises(LLMError, match="cut off"):
        claude(lambda r: message('{"overview": "trunc', "max_tokens")).complete(
            "s", "u", schema=schemas.SUMMARY_SCHEMA
        )
    with pytest.raises(LLMError, match="declined"):
        claude(lambda r: message("", "refusal")).complete("s", "u", schema=schemas.SUMMARY_SCHEMA)


# ---- Ollama ----------------------------------------------------------------------------


def ollama(responses):
    """responses: list of (status, body) returned in order; returns (provider, sent_payloads)."""
    sent = []
    queue = list(responses)

    def transport(url, data, headers, timeout):
        sent.append(json.loads(data))
        status, body = queue.pop(0)
        return status, json.dumps(body).encode()

    return OllamaProvider(Config(provider="ollama"), transport=transport), sent


def test_ollama_receives_the_schema_as_its_format():
    provider, sent = ollama([(200, {"message": {"content": "{}"}})])
    provider.complete("s", "u", json_mode=True, schema=schemas.SYLLABUS_SCHEMA)
    assert sent[0]["format"] == schemas.SYLLABUS_SCHEMA


def test_old_ollama_that_rejects_a_schema_gets_plain_json_mode(caplog):
    provider, sent = ollama([(400, {"error": "invalid format"}), (200, {"message": {"content": "{}"}})])
    with caplog.at_level(logging.WARNING):
        assert provider.complete("s", "u", json_mode=True, schema=schemas.SUMMARY_SCHEMA) == "{}"
    assert [p["format"] for p in sent] == [schemas.SUMMARY_SCHEMA, "json"]
    assert "Update Ollama" in caplog.text


def test_other_ollama_errors_are_not_retried():
    provider, sent = ollama([(500, {"error": "model crashed"})])
    with pytest.raises(LLMError, match="model crashed"):
        provider.complete("s", "u", schema=schemas.SUMMARY_SCHEMA)
    assert len(sent) == 1


# ---- the callers use the right schema --------------------------------------------------


def test_summary_requests_carry_the_summary_schema():
    provider = FakeProvider([summary_json()])
    summarize_chapter(provider, "text", course="BIO", number=1, title="")
    assert provider.schemas == [schemas.SUMMARY_SCHEMA]


def test_long_chapters_use_the_summary_schema_per_part_and_the_combine_schema_to_merge():
    def responder(user):
        if "<chapter_part>" in user:
            return summary_json()
        return json.dumps({"overview": "o", "key_points": ["k"]})

    provider = FakeProvider(responder, max_input_chars=300)
    summarize_chapter(provider, "\n\n".join("word " * 40 for _ in range(6)), course="BIO", number=1, title="")
    assert provider.schemas[0] == schemas.SUMMARY_SCHEMA  # the first calls summarize the parts
    assert provider.schemas[-1] == schemas.COMBINE_SCHEMA  # the last one merges them
    assert all(s in (schemas.SUMMARY_SCHEMA, schemas.COMBINE_SCHEMA) for s in provider.schemas)


def test_syllabus_requests_carry_the_syllabus_schema():
    provider = FakeProvider([json.dumps({"readings": []})])
    extract_schedule(provider, "text", today=date(2026, 9, 1))
    assert provider.schemas == [schemas.SYLLABUS_SCHEMA]


def test_a_retry_keeps_the_schema():
    provider = FakeProvider(["not json", summary_json()])
    complete_structured(provider, "s", "u", lambda d: d, schema=schemas.SUMMARY_SCHEMA)
    assert provider.schemas == [schemas.SUMMARY_SCHEMA, schemas.SUMMARY_SCHEMA]


# ---- the failure that prompted this, and its diagnostics -------------------------------

# A definition that quotes a term: "so-called "dark energy"". The inner quotes end the string early.
UNESCAPED = (
    '{"overview": "Cosmology.", "key_points": ["a"], '
    '"definitions": [{"term": "Dark energy", "definition": "The so-called "dark energy" that speeds expansion."}]}'
)


def test_an_unescaped_quote_makes_free_form_json_unparseable():
    with pytest.raises(ValueError, match="Expecting ',' delimiter"):
        parse_json_object(UNESCAPED)


def test_unusable_replies_are_logged_with_the_text_around_the_problem(caplog):
    provider = FakeProvider([UNESCAPED, UNESCAPED])
    with caplog.at_level(logging.WARNING), pytest.raises(LLMError, match="usable JSON"):
        complete_structured(provider, "s", "u", lambda d: d)
    assert caplog.text.count("sent unusable JSON") == 2
    assert "dark energy" in caplog.text  # the culprit is visible in `docker compose logs`


def test_raw_newlines_inside_strings_are_tolerated():
    assert parse_json_object('{"overview": "line one\nline two"}') == {"overview": "line one\nline two"}

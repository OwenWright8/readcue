import json
from urllib.parse import parse_qs

import pytest

from readcue.config import Config
from readcue.errors import ConfigError, LLMError, NotifyError
from readcue.llm.ollama import OllamaProvider
from readcue.notify import PushoverNotifier


def pushover(cfg, status=200, body=None, error=None):
    calls = []

    def transport(url, data, headers, timeout):
        calls.append((url, parse_qs(data.decode())))
        if error:
            raise error
        return status, json.dumps(body if body is not None else {"status": 1}).encode()

    return PushoverNotifier(cfg, transport=transport), calls


def test_pushover_payload(cfg):
    notifier, calls = pushover(cfg)
    cfg.pushover_device = "phone"
    notifier.send("Title", "Body", url="http://x/summary/1", url_title="Open")
    url, form = calls[0]
    assert url == "https://api.pushover.net/1/messages.json"
    assert form["token"] == ["tok"] and form["user"] == ["usr"] and form["device"] == ["phone"]
    assert form["title"] == ["Title"] and form["url"] == ["http://x/summary/1"]
    assert "priority" not in form


def test_pushover_truncates_long_messages(cfg):
    notifier, calls = pushover(cfg)
    notifier.send("t" * 400, "m" * 3000)
    form = calls[0][1]
    assert len(form["title"][0]) == 250 and len(form["message"][0]) == 1024


def test_pushover_errors(cfg):
    notifier, _ = pushover(cfg, status=400, body={"status": 0, "errors": ["application token is invalid"]})
    with pytest.raises(NotifyError, match="application token is invalid"):
        notifier.send("t", "m")
    notifier, _ = pushover(cfg, error=OSError("no route"))
    with pytest.raises(NotifyError, match="Couldn't reach Pushover"):
        notifier.send("t", "m")


def test_pushover_requires_credentials():
    notifier = PushoverNotifier(Config())
    assert not notifier.configured
    with pytest.raises(NotifyError, match="isn't configured"):
        notifier.send("t", "m")


def ollama(cfg, status=200, body=None, error=None):
    calls = []

    def transport(url, data, headers, timeout):
        calls.append((url, json.loads(data), headers))
        if error:
            raise error
        return status, json.dumps(body).encode()

    return OllamaProvider(cfg, transport=transport), calls


def test_ollama_request_shape_and_host_normalisation(cfg):
    cfg.ollama_host = "ollama:11434/"  # OLLAMA_HOST is often written without a scheme
    cfg.ollama_api_key = "secret"
    provider, calls = ollama(cfg, body={"message": {"content": "hi"}})
    assert provider.complete("sys", "user", json_mode=True) == "hi"
    url, payload, headers = calls[0]
    assert url == "http://ollama:11434/api/chat"
    assert payload["format"] == "json" and payload["stream"] is False
    assert payload["options"]["num_ctx"] == cfg.ollama_num_ctx
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert headers["Authorization"] == "Bearer secret"
    assert provider.max_input_chars == int(cfg.ollama_num_ctx * 1.75)


def test_ollama_errors(cfg):
    provider, _ = ollama(cfg, status=404, body={"error": "model 'nope' not found"})
    with pytest.raises(LLMError, match="model 'nope' not found"):
        provider.complete("s", "u")
    provider, _ = ollama(cfg, error=OSError("connection refused"))
    with pytest.raises(LLMError, match="Couldn't reach Ollama"):
        provider.complete("s", "u")
    provider, _ = ollama(cfg, body={"message": {"content": "  "}})
    with pytest.raises(LLMError, match="empty"):
        provider.complete("s", "u")


def test_config_from_env():
    cfg = Config.from_env(
        {
            "READCUE_LLM_PROVIDER": "Anthropic",
            "READCUE_NOTIFY_TIME": "07:30",
            "READCUE_NOTIFY_DAYS_BEFORE": "2",
            "OLLAMA_MODEL": "",  # blank values fall back to defaults
            "READCUE_DATA_DIR": "/data",
        }
    )
    assert cfg.provider == "anthropic" and cfg.model_name == "claude-sonnet-5"
    assert (cfg.notify_time.hour, cfg.notify_time.minute, cfg.notify_days_before) == (7, 30, 2)
    assert cfg.ollama_model == "llama3.1:8b" and str(cfg.db_path) == "/data/readcue.db"


def test_defaults_are_claude_sonnet_with_a_three_day_summary_window():
    cfg = Config.from_env({})
    assert (cfg.provider, cfg.model_name) == ("anthropic", "claude-sonnet-5")
    assert cfg.summarize_days_before == 3 and cfg.notify_days_before == 1 and cfg.ocr_lang == "eng"
    assert (
        Config.from_env({"READCUE_SUMMARIZE_DAYS_BEFORE": "5", "READCUE_OCR_LANG": "eng+spa"}).ocr_lang
        == "eng+spa"
    )


@pytest.mark.parametrize(
    "env",
    [
        {"READCUE_LLM_PROVIDER": "gpt"},
        {"READCUE_SUMMARIZE_DAYS_BEFORE": "-1"},
        {"READCUE_OCR_LANG": "eng; rm -rf /"},
        {"OLLAMA_NUM_CTX": "lots"},
        {"READCUE_NOTIFY_TIME": "morning"},
        {"PUSHOVER_PRIORITY": "2"},
    ],
)
def test_config_rejects_bad_values(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)

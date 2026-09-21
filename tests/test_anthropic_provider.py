import pytest

from readcue.config import Config
from readcue.errors import LLMError
from readcue.llm.anthropic_provider import AnthropicProvider


def test_missing_credentials_is_a_clean_error(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `ant auth login` profile to find
    provider = AnthropicProvider(Config(provider="anthropic"))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        provider.complete("system", "user")

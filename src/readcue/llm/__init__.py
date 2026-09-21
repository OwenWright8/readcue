from __future__ import annotations

from ..config import Config
from ..errors import ConfigError
from .base import LLMProvider


def make_provider(cfg: Config) -> LLMProvider:
    if cfg.provider == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(cfg)
    if cfg.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(cfg)
    raise ConfigError(f"Unknown LLM provider {cfg.provider!r}.")

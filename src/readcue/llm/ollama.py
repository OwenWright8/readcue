from __future__ import annotations

import json
import logging
from collections.abc import Callable

from .. import http
from ..config import Config
from ..errors import LLMError

log = logging.getLogger(__name__)

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class OllamaProvider:
    name = "ollama"

    def __init__(self, cfg: Config, transport: Transport = http.post):
        host = cfg.ollama_host.rstrip("/")
        self.host = host if "://" in host else f"http://{host}"  # OLLAMA_HOST is often a bare host:port
        self.model = cfg.ollama_model
        self.num_ctx = cfg.ollama_num_ctx
        self.timeout = cfg.ollama_timeout
        self.api_key = cfg.ollama_api_key
        # Roughly half the context window for source text (~3.5 chars/token); the rest holds
        # the prompt and the reply.
        self.max_input_chars = int(self.num_ctx * 1.75)
        self._transport = transport

    @property
    def label(self) -> str:
        return f"ollama:{self.model}"

    def complete(self, system: str, user: str, *, json_mode: bool = False, schema: dict | None = None) -> str:
        # A schema constrains the reply to that shape (Ollama 0.5+); otherwise plain JSON mode.
        reply_format = schema if schema is not None else ("json" if json_mode else None)
        status, raw = self._chat(system, user, reply_format)
        if status == 400 and isinstance(reply_format, dict):
            log.warning(
                "Ollama rejected the JSON schema (%s); retrying in plain JSON mode. Update Ollama.",
                _error_text(raw),
            )
            status, raw = self._chat(system, user, "json")
        if status != 200:
            raise LLMError(f"Ollama returned HTTP {status}: {_error_text(raw)}")
        try:
            content = json.loads(raw)["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise LLMError("Ollama sent a response in an unexpected format.") from None
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Ollama returned an empty response.")
        return content

    def _chat(self, system: str, user: str, reply_format: dict | str | None) -> tuple[int, bytes]:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2},
        }
        if reply_format is not None:
            payload["format"] = reply_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            return self._transport(
                f"{self.host}/api/chat", json.dumps(payload).encode(), headers, self.timeout
            )
        except OSError as e:
            raise LLMError(f"Couldn't reach Ollama at {self.host}: {e}") from e


def _error_text(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("error") or raw[:200])
    except (ValueError, AttributeError):
        return raw[:200].decode("utf-8", errors="replace")

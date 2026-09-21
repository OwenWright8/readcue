from __future__ import annotations

import base64
import logging

import anthropic

from ..config import Config
from ..errors import LLMError
from .base import Image

log = logging.getLogger(__name__)


def _detail(error: anthropic.APIStatusError) -> str:
    """The API's own sentence ("prompt is too long"), without the SDK's "Error code: 400 - {...}" wrapper."""
    body = error.body
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
    return error.message


class _StructuredRejected(LLMError):
    """Claude answered 400 to a request that carried a JSON schema."""


class AnthropicProvider:
    name = "anthropic"
    # 1M-token context; ~600k characters leaves ample room for the prompt and the reply.
    max_input_chars = 600_000
    supports_images = True

    def __init__(self, cfg: Config):
        self.model = cfg.anthropic_model
        self.max_tokens = cfg.anthropic_max_tokens
        # An empty key makes the SDK use ANTHROPIC_API_KEY or its other credential sources.
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None)

    @property
    def label(self) -> str:
        return f"anthropic:{self.model}"

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        schema: dict | None = None,
        images: list[Image] | None = None,
    ) -> str:
        if schema is not None:
            try:
                return self._text(self._send(system, user, schema, images))
            except (
                _StructuredRejected
            ) as e:  # e.g. a model without structured outputs: fall back to plain JSON
                log.warning(
                    "Claude rejected structured outputs for %s (%s); asking for plain JSON", self.model, e
                )
        return self._text(self._send(system, user, None, images))

    @staticmethod
    def _content(user: str, images: list[Image] | None):
        """A plain string, or (with images) each labelled image followed by the text."""
        if not images:
            return user
        blocks: list[dict] = []
        for label, png in images:
            blocks.append({"type": "text", "text": label})
            data = base64.standard_b64encode(png).decode("ascii")
            blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
            )
        blocks.append({"type": "text", "text": user})
        return blocks

    def _send(self, system: str, user: str, schema: dict | None, images: list[Image] | None = None):
        extra = {}
        if schema is not None:
            # Constrained decoding: the reply is guaranteed to be valid JSON matching the schema.
            extra["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        try:
            return self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": self._content(user, images)}],
                **extra,
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API rejected the API key. Check ANTHROPIC_API_KEY.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Claude API doesn't know the model {self.model!r}.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API rate limit hit. Try again in a minute.") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Couldn't reach the Claude API: {e}") from e
        except anthropic.BadRequestError as e:
            if schema is not None:
                raise _StructuredRejected(_detail(e)) from e
            raise LLMError(f"Claude API error (400): {_detail(e)}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error ({e.status_code}): {_detail(e)}") from e
        except TypeError as e:
            # The SDK signals "no credentials found anywhere" with a bare TypeError.
            if "authentication method" not in str(e):
                raise
            raise LLMError("No Claude API credentials found. Set ANTHROPIC_API_KEY.") from e

    @staticmethod
    def _text(response) -> str:
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined to process this text.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's reply was cut off. Raise ANTHROPIC_MAX_TOKENS.")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError("Claude returned an empty response.")
        return text

from __future__ import annotations

import anthropic

from ..config import Config
from ..errors import LLMError


class AnthropicProvider:
    name = "anthropic"
    # 1M-token context; ~600k characters leaves ample room for the prompt and the reply.
    max_input_chars = 600_000

    def __init__(self, cfg: Config):
        self.model = cfg.anthropic_model
        self.max_tokens = cfg.anthropic_max_tokens
        # An empty key makes the SDK use ANTHROPIC_API_KEY or its other credential sources.
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None)

    @property
    def label(self) -> str:
        return f"anthropic:{self.model}"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API rejected the API key. Check ANTHROPIC_API_KEY.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Claude API doesn't know the model {self.model!r}.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API rate limit hit. Try again in a minute.") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Couldn't reach the Claude API: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error ({e.status_code}): {e.message}") from e
        except TypeError as e:
            # The SDK signals "no credentials found anywhere" with a bare TypeError.
            if "authentication method" not in str(e):
                raise
            raise LLMError("No Claude API credentials found. Set ANTHROPIC_API_KEY.") from e

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined to process this text.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's reply was cut off. Raise ANTHROPIC_MAX_TOKENS.")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError("Claude returned an empty response.")
        return text

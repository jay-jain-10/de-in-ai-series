"""LLM client abstraction so the gateway never imports the Anthropic SDK directly."""

from dataclasses import dataclass
from typing import Protocol


class LLMTransientError(Exception):
    """Rate limit / 5xx / network failure. Counts toward the circuit breaker."""


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(Protocol):
    def complete(self, model: str, prompt: str, max_tokens: int) -> LLMResult: ...


class AnthropicLLMClient:
    """Production adapter over the Anthropic SDK (same conventions as Article 1)."""

    def __init__(self, api_key: str = "", client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        self._client = client

    def complete(self, model: str, prompt: str, max_tokens: int) -> LLMResult:
        import anthropic
        try:
            msg = self._client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
        except (anthropic.RateLimitError, anthropic.APIConnectionError,  # incl. APITimeoutError
                anthropic.InternalServerError) as exc:
            raise LLMTransientError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            # 529 overloaded / other 5xx are transient; 4xx (bad request, auth, not found) are
            # NOT: retrying them wastes calls, and counting them toward the shared breaker would
            # let one team's malformed requests open the circuit for every team.
            if exc.status_code >= 500:
                raise LLMTransientError(str(exc)) from exc
            raise
        return LLMResult(text=msg.content[0].text,
                         input_tokens=msg.usage.input_tokens,
                         output_tokens=msg.usage.output_tokens)

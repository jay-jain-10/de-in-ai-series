"""Shared helpers for calling Claude and parsing JSON replies."""

import json
import logging
import time

import anthropic

logger = logging.getLogger(__name__)


class UsageTracker:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0

    def add(self, response):
        self.requests += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens


def strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        raw = raw.rsplit("```", 1)[0].strip()
    return raw


def call_claude(client, model: str, prompt: str, max_tokens: int, tracker: UsageTracker,
                max_retries: int = 3, sleep=time.sleep) -> str:
    """Call the Messages API with exponential backoff on rate limits.

    Returns raw text. Raises the last anthropic error after max_retries.
    """
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            tracker.add(response)
            return strip_fences(response.content[0].text)
        except anthropic.RateLimitError:
            if attempt == max_retries - 1:
                raise
            sleep(2 ** (attempt + 1))
        except anthropic.APIError:
            if attempt == max_retries - 1:
                raise
            sleep(2 ** (attempt + 1))  # back off on 5xx / connection errors too
    raise RuntimeError("unreachable")


def parse_json(raw: str):
    return json.loads(strip_fences(raw))

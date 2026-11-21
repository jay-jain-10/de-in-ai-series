"""Cost guardrail: lower the AI sampling rate when daily spend exceeds budget.

Article: "implement cost guardrails (circuit breakers that reduce sample rate
if daily spend exceeds threshold)". Spend resets at each UTC day boundary.
Sampling is deterministic per event_id (hash bucket), so retries and
replays make the same decision.
"""

import hashlib
import time

from src.config import CostConfig, AnthropicConfig


def sample_bucket(event_id: str) -> float:
    """Stable value in [0, 1) derived from event_id."""
    h = int(hashlib.sha256(event_id.encode()).hexdigest()[:8], 16)
    return h / 0x100000000


class CostGuard:
    def __init__(self, cost_cfg: CostConfig = None, api_cfg: AnthropicConfig = None, clock=time.time):
        self.cfg = cost_cfg or CostConfig()
        self.api = api_cfg or AnthropicConfig()
        self.clock = clock
        self.day = self._day()
        self.spend_usd = 0.0

    def _day(self):
        return int(self.clock() // 86400)

    def _roll(self):
        if self._day() != self.day:
            self.day = self._day()
            self.spend_usd = 0.0

    def record_usage(self, input_tokens: int, output_tokens: int) -> float:
        self._roll()
        cost = (input_tokens / 1e6) * self.api.input_price_per_mtok + (
            output_tokens / 1e6
        ) * self.api.output_price_per_mtok
        self.spend_usd += cost
        return cost

    def sample_rate(self) -> float:
        self._roll()
        ratio = self.spend_usd / self.cfg.daily_budget_usd if self.cfg.daily_budget_usd > 0 else 0
        for threshold, rate in sorted(self.cfg.tiers, reverse=True):
            if ratio >= threshold:
                return rate
        return 1.0

    def should_sample(self, event_id: str, extra_rate: float = 1.0) -> bool:
        """Combine cost-based rate with another rate (e.g. backpressure); lowest wins."""
        return sample_bucket(event_id) < min(self.sample_rate(), extra_rate)

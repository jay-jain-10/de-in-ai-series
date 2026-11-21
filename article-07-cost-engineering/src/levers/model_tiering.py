"""Lever 2: Model tiering.

Score request complexity 0-10 from cheap heuristics, route:
  score < 3 -> Haiku, 3 <= score < 7 -> Sonnet, >= 7 -> Opus.
Fallback: if the Haiku output fails downstream checks, retry on Sonnet.
"""

import re
from dataclasses import dataclass
from typing import Callable, Optional

from src.config import AnthropicConfig, LeverConfig

DOMAIN_TERMS = {
    "refund", "chargeback", "dispute", "fraud", "kyc", "aml", "compliance",
    "regulator", "lawsuit", "legal", "audit", "wire", "ach", "transaction",
    "claim", "policy", "invoice", "escalate",
}
REASONING_WORDS = re.compile(r"\b(why|how)\b", re.I)
_ENTITY = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b|\$\d[\d,\.]*|\b\d{4,}\b")


def complexity_score(text: str) -> float:
    """0-10: length (0-4) + domain keyword density (0-2) + entity count (0-2)
    + reasoning required (0-2)."""
    text = text or ""
    words = text.split()
    length = min(len(words) / 100, 1.0) * 4
    hits = sum(1 for w in re.findall(r"[a-z]+", text.lower()) if w in DOMAIN_TERMS)
    density = min(hits / max(len(words), 1) * 20, 1.0) * 2
    entities = min(len(_ENTITY.findall(text)) / 10, 1.0) * 2
    reasoning = 2.0 if REASONING_WORDS.search(text) else 0.0
    return round(min(length + density + entities + reasoning, 10.0), 2)


@dataclass
class Route:
    model: str
    tier: str
    score: float


class ModelRouter:
    def __init__(self, models: AnthropicConfig = None, levers: LeverConfig = None):
        self.models = models or AnthropicConfig()
        self.levers = levers or LeverConfig()

    def route(self, text: str) -> Route:
        score = complexity_score(text)
        if score < self.levers.haiku_max_score:
            return Route(self.models.haiku_model, "haiku", score)
        if score < self.levers.sonnet_max_score:
            return Route(self.models.sonnet_model, "sonnet", score)
        return Route(self.models.opus_model, "opus", score)

    def fallback(self, route: Route) -> Optional[Route]:
        """Haiku -> Sonnet on failed validation. Sonnet/Opus have no fallback."""
        if route.tier == "haiku":
            return Route(self.models.sonnet_model, "sonnet", route.score)
        return None


def weighted_cost_ratio(haiku_share: float, haiku_price: float, other_price: float,
                        reference_price: float) -> float:
    """(share*haiku + (1-share)*other) / reference, as in the article's tiering step."""
    return (haiku_share * haiku_price + (1 - haiku_share) * other_price) / reference_price

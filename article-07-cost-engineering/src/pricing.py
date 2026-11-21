"""Model price table and cost math.

Input prices are the article's ($/1M tokens): Opus $15, Sonnet $3, Haiku $0.80.
Output prices are NOT given in the article; we assume the usual 5x input
multiplier (Haiku $4, Sonnet $15, Opus $75). Change PRICING to match your contract.
"""

from dataclasses import dataclass
from typing import Dict

from src.config import AnthropicConfig

BATCH_DISCOUNT = 0.50


@dataclass(frozen=True)
class ModelPrice:
    tier: str
    input_per_m: float
    output_per_m: float


def build_pricing(cfg: AnthropicConfig = None) -> Dict[str, ModelPrice]:
    cfg = cfg or AnthropicConfig()
    return {
        cfg.haiku_model: ModelPrice("haiku", 0.80, 4.00),
        cfg.sonnet_model: ModelPrice("sonnet", 3.00, 15.00),
        cfg.opus_model: ModelPrice("opus", 15.00, 75.00),
    }


PRICING = build_pricing()
TIER_INPUT_PRICE = {"haiku": 0.80, "sonnet": 3.00, "opus": 15.00}


def price_for(model: str, pricing: Dict[str, ModelPrice] = None) -> ModelPrice:
    pricing = pricing or PRICING
    if model in pricing:
        return pricing[model]
    # tolerate undated aliases, e.g. "claude-haiku-4-5"
    for key, price in pricing.items():
        if model and (key.startswith(model) or model.startswith(key)):
            return price
    lowered = (model or "").lower()
    for tier in ("haiku", "sonnet", "opus"):
        if tier in lowered:
            return next(p for p in pricing.values() if p.tier == tier)
    raise KeyError(f"Unknown model for pricing: {model}")


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    batch: bool = False,
    pricing: Dict[str, ModelPrice] = None,
) -> float:
    p = price_for(model, pricing)
    cost = input_tokens / 1_000_000 * p.input_per_m + output_tokens / 1_000_000 * p.output_per_m
    if batch:
        cost *= BATCH_DISCOUNT
    return round(cost, 8)


def model_cost_ratio(a: str, b: str) -> float:
    """Input price ratio between two tiers (Opus/Haiku = 18.75x in the article)."""
    return TIER_INPUT_PRICE[a] / TIER_INPUT_PRICE[b]

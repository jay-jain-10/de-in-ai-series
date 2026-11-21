"""Waterfall / savings calculator reproducing the article's numbers.

Scenario A (10M records/month, Opus baseline):
  $150K -> cache -40% -> $90K -> tiering x0.47 -> $42K -> prompt -25% -> $31.5K
  -> batching -50% -> $15.75K  (89.5% reduction)

Scenario B (Article 1 support-ticket worked example, 50K tickets):
  API $116 -> $87 -> $48 -> $33.60 -> $16.80, plus $300 infra.

Both are implemented FAITHFULLY (article constants), and `reconcile_*` functions
recompute the arithmetic independently and list every discrepancy.
"""

from dataclasses import dataclass, field
from typing import Dict, List

from src.levers.model_tiering import weighted_cost_ratio


# ─── Scenario A: 10M-record waterfall ────────────────────────────────

@dataclass
class WaterfallAssumptions:
    records: int = 10_000_000
    opus_price_per_m: float = 15.0
    # The article writes "10M records x $15/M tokens = $150K", which only holds at
    # 1,000 tokens per record (10M x 1,000 = 10B tokens = 10,000 M-tokens x $15).
    tokens_per_record: int = 1000
    cache_hit_rate: float = 0.40
    haiku_share: float = 0.60
    haiku_price: float = 0.80
    sonnet_price: float = 3.00
    # Article states 0.47 explicitly ("= 0.47"); the formula it shows gives 0.56.
    tier_factor: float = 0.47
    prompt_reduction: float = 0.25
    batch_discount: float = 0.50
    # Article shows $42K (rounds $90K x 0.47 = $42.3K) before carrying on.
    round_tier_stage_to: float = 1000.0

    @property
    def baseline(self) -> float:
        return self.records * self.tokens_per_record / 1_000_000 * self.opus_price_per_m


@dataclass
class Stage:
    name: str
    cost: float
    delta_pct: float  # change vs previous stage (negative = savings)


@dataclass
class Waterfall:
    stages: List[Stage]
    assumptions: WaterfallAssumptions

    @property
    def baseline(self) -> float:
        return self.stages[0].cost

    @property
    def final(self) -> float:
        return self.stages[-1].cost

    @property
    def total_reduction(self) -> float:
        return 1 - self.final / self.baseline

    def as_dict(self) -> Dict[str, float]:
        return {s.name: s.cost for s in self.stages}


def computed_tier_factor(a: WaterfallAssumptions = None) -> float:
    """The article's formula: (60% x $0.80 + 40% x $3) / $3 = 0.56 (not 0.47)."""
    a = a or WaterfallAssumptions()
    return weighted_cost_ratio(a.haiku_share, a.haiku_price, a.sonnet_price, a.sonnet_price)


def compute_waterfall(a: WaterfallAssumptions = None, tier_factor: float = None) -> Waterfall:
    a = a or WaterfallAssumptions()
    tf = a.tier_factor if tier_factor is None else tier_factor
    base = a.baseline
    stages = [Stage("baseline", base, 0.0)]

    cache = base * (1 - a.cache_hit_rate)
    stages.append(Stage("after_cache", cache, -a.cache_hit_rate))

    tier = cache * tf
    if a.round_tier_stage_to:
        tier = round(tier / a.round_tier_stage_to) * a.round_tier_stage_to
    stages.append(Stage("after_tier", tier, tier / cache - 1))

    opt = tier * (1 - a.prompt_reduction)
    stages.append(Stage("after_opt", opt, -a.prompt_reduction))

    final = opt * (1 - a.batch_discount)
    stages.append(Stage("final", final, -a.batch_discount))
    return Waterfall(stages, a)


def article_waterfall() -> Waterfall:
    """Exactly the article's sequence: 150000, 90000, 42000, 31500, 15750."""
    return compute_waterfall(WaterfallAssumptions())


def engineering_payback(wf: Waterfall, hours: float = 80, rate: float = 150.0) -> Dict[str, float]:
    """Article: ~80 hours ($12K one-time), payback < 1 month."""
    investment = hours * rate
    monthly_savings = wf.baseline - wf.final
    return {"investment_usd": investment, "monthly_savings_usd": monthly_savings,
            "payback_months": investment / monthly_savings if monthly_savings else float("inf")}


def reconcile_waterfall() -> List[str]:
    """Independent recomputation vs the article's stated figures."""
    a = WaterfallAssumptions()
    notes: List[str] = []
    tf = computed_tier_factor(a)
    if abs(tf - a.tier_factor) > 1e-9:
        notes.append(
            f"Tiering factor: article says (60% x $0.80 + 40% x $3)/$3 = 0.47, "
            f"but the formula evaluates to {tf:.2f}.")
        # Independent recomputation: no $1K rounding of the tier stage.
        unrounded = WaterfallAssumptions(round_tier_stage_to=0)
        honest = compute_waterfall(unrounded, tier_factor=tf)
        notes.append(
            f"With {tf:.2f} the waterfall is {[round(s.cost) for s in honest.stages]} "
            f"-> reduction {honest.total_reduction:.1%} (article: 89.5%).")
    notes.append(
        "Tiering step compares against Sonnet's $3 even though the baseline is Opus ($15); "
        "a true Opus-relative blend of 60% Haiku/40% Sonnet would be 0.112, not 0.47/0.56.")
    notes.append(
        "Baseline '10M records x $15/M tokens = $150K' implies 1,000 tokens/record (unstated).")
    notes.append(
        "Tiering narrative says '80% to Haiku' / '76% reduction' but the waterfall uses 60% to Haiku.")
    notes.append(
        "Batching is described as '50% discount on non-cached work' yet applied to the whole "
        "post-cache cost; harmless here because cache hits cost $0 already.")
    notes.append(
        "Section headers quote different ranges: 'cuts costs by 70-80%' (intro) vs 89.5% (waterfall); "
        "'Model Tiering (60-70%)' vs 53% in the summary bullet.")
    return notes


# ─── Scenario B: Article 1 ticket pipeline worked example ────────────

@dataclass
class TicketExample:
    baseline_api: float = 116.0
    infra: float = 300.0
    tickets: int = 50_000
    cache_hit_rate: float = 0.25
    haiku_cost: float = 25.0      # sentiment on 50K tickets
    sonnet_cost: float = 23.0     # escalation risk on 10K complex cases
    prompt_reduction: float = 0.30
    batch_discount: float = 0.50


def ticket_worked_example(e: TicketExample = None) -> Dict[str, float]:
    e = e or TicketExample()
    after_cache = e.baseline_api * (1 - e.cache_hit_rate)             # 87
    after_tier = e.haiku_cost + e.sonnet_cost                          # 48 (article value)
    after_opt = after_tier * (1 - e.prompt_reduction)                  # 33.60
    after_batch = after_opt * (1 - e.batch_discount)                   # 16.80
    return {
        "baseline_api": e.baseline_api,
        "after_cache": round(after_cache, 2),
        "after_tier": round(after_tier, 2),
        "after_opt": round(after_opt, 2),
        "final_api": round(after_batch, 2),
        "total_before": e.baseline_api + e.infra,
        "total_after": round(after_batch + e.infra, 2),
        "api_reduction_pct": round((1 - after_batch / e.baseline_api) * 100, 1),
        "total_reduction_pct": round((1 - (after_batch + e.infra) / (e.baseline_api + e.infra)) * 100, 1),
        "cost_per_ticket_before": e.baseline_api / e.tickets,
        "cost_per_ticket_after": after_batch / e.tickets,
    }


# What the article claims, for reconciliation.
ARTICLE_TICKET_CLAIMS = {
    "lever1_savings": 29.0,
    "levers12_savings": 97.0,
    "levers12_pct_of_api": 68.0,
    "levers123_savings": 111.40,
    "levers123_pct_of_api": 96.0,
    "all_savings": 399.20,
    "all_pct_of_api": 96.0,
    "cost_at_100k": 633.60,
}


def reconcile_ticket_example(e: TicketExample = None) -> List[str]:
    e = e or TicketExample()
    r = ticket_worked_example(e)
    c = ARTICLE_TICKET_CLAIMS
    base = e.baseline_api
    notes: List[str] = []

    def pct(x): return round(x / base * 100, 1)

    notes.append(
        f"Tiering ($25 Haiku + $23 Sonnet = $48) is measured against the $116 baseline, not the "
        f"post-cache $87, so levers 1 and 2 are not actually compounded (cache savings are discarded).")
    s12 = base - r["after_tier"]
    if abs(s12 - c["levers12_savings"]) > 0.01:
        notes.append(f"'Levers 1+2 saves $97 (68% of API)': recomputed ${s12:.2f} ({pct(s12)}%); "
                     f"$97 = $29 + $68 double counts, and $68 is the 1+2 saving by itself.")
    s123 = base - r["after_opt"]
    if abs(s123 - c["levers123_savings"]) > 0.01:
        notes.append(f"'Levers 1+2+3 saves $111.40 (96%)': recomputed ${s123:.2f} ({pct(s123)}%).")
    sall = base - r["final_api"]
    if abs(sall - c["all_savings"]) > 0.01:
        notes.append(f"'All four saves $399.20 (96%)': recomputed ${sall:.2f} ({pct(sall)}% of API, "
                     f"consistent with the article's own 85% headline); $399.20 = $416 - $16.80 mixes "
                     f"total and API cost.")
    cost_100k = r["final_api"] * 2 + e.infra
    notes.append(f"'100K tickets costs $633.60' doubles the $300 infra; with infra fixed it is "
                 f"${cost_100k:.2f}.")
    notes.append("Section says 'Lever 2: 59% reduction' ($48/$116 -> 58.6%) and 'Lever 1: 25%'; "
                 "these percentages are vs baseline, not sequential.")
    return notes

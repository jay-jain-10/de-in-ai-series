"""
Monthly calibration protocol (Article 6, "Calibration and Trust").

1. Pull 200 random records flagged by the semantic validator.
2. Two independent reviewers assess each (agree_with_flag: bool).
3. Precision = both reviewers agree with the AI flag / total flagged.
4. Cohen's kappa between the two reviewers. kappa < 0.7 -> standardise the review
   process before trusting precision.
5. Precision < 80% for a domain -> add few-shot examples, refine prompt, or
   adjust the confidence threshold.
"""

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from src.config import CALIBRATION_SAMPLE_SIZE, KAPPA_MIN, PRECISION_MIN


def draw_calibration_sample(
    flagged: Sequence[dict], n: int = CALIBRATION_SAMPLE_SIZE, seed: Optional[int] = None
) -> List[dict]:
    """Random sample (without replacement) of L2-flagged records."""
    flagged = list(flagged)
    if len(flagged) <= n:
        return flagged
    return random.Random(seed).sample(flagged, n)


def cohens_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Cohen's kappa for two raters over binary labels."""
    if len(a) != len(b):
        raise ValueError("rater label lists must be the same length")
    n = len(a)
    if n == 0:
        raise ValueError("no ratings")
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    pa, pb = sum(a) / n, sum(b) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    if expected == 1.0:
        # Both raters used a single identical label throughout: perfect agreement.
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def precision_both_agree(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Records where both reviewers agree with the AI flag / total flagged."""
    if len(a) != len(b) or not a:
        raise ValueError("need equal, non-empty rater lists")
    return sum(1 for x, y in zip(a, b) if x and y) / len(a)


@dataclass
class CalibrationReport:
    domain: str
    n_reviewed: int
    precision: float
    kappa: float
    precision_ok: bool
    kappa_ok: bool
    trustworthy: bool
    actions: List[str] = field(default_factory=list)


def evaluate_calibration(
    domain: str, reviewer_a: Sequence[bool], reviewer_b: Sequence[bool],
    kappa_min: float = KAPPA_MIN, precision_min: float = PRECISION_MIN,
) -> CalibrationReport:
    kappa = cohens_kappa(reviewer_a, reviewer_b)
    precision = precision_both_agree(reviewer_a, reviewer_b)
    kappa_ok = kappa >= kappa_min
    precision_ok = precision >= precision_min
    actions = []
    if not kappa_ok:
        actions.append("standardize_review_process: kappa < 0.7, precision numbers not yet trustworthy")
    if not precision_ok:
        actions += [
            "add_few_shot_examples",
            "refine_prompt",
            "adjust_confidence_threshold",
        ]
    return CalibrationReport(
        domain=domain, n_reviewed=len(reviewer_a), precision=round(precision, 4),
        kappa=round(kappa, 4), precision_ok=precision_ok, kappa_ok=kappa_ok,
        trustworthy=kappa_ok, actions=actions,
    )


def suggest_threshold(
    scored: Sequence[Dict], target_precision: float = PRECISION_MIN, grid: Optional[Sequence[float]] = None
) -> Optional[float]:
    """Lowest confidence threshold (from the grid) whose precision on reviewed
    flags meets the target. `scored` items: {"confidence": float, "confirmed": bool}.
    Returns None if no threshold on the grid reaches the target."""
    grid = grid or [round(0.5 + 0.05 * i, 2) for i in range(10)]
    for t in sorted(grid):
        kept = [s for s in scored if s["confidence"] >= t]
        if kept and sum(s["confirmed"] for s in kept) / len(kept) >= target_precision:
            return t
    return None


def precision_recall(flagged_ids: Sequence[str], true_error_ids: Sequence[str]) -> Dict[str, float]:
    """Precision and recall of a validator against a human-labeled audit set.

    Recall needs known errors among *unflagged* records too, so feed it a
    labeled audit sample (e.g. the monthly random audit), not just flags.
    """
    flagged, truth = set(flagged_ids), set(true_error_ids)
    tp = len(flagged & truth)
    precision = tp / len(flagged) if flagged else 0.0
    recall = tp / len(truth) if truth else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "true_positives": tp}

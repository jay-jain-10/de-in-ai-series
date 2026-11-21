"""Cost model from the article (10K contracts / quarter)."""

from typing import Dict

TEXTRACT_PER_1000_PAGES = 1.50
HAIKU_INPUT_PER_M = 0.80    # article's pricing assumption
SONNET_INPUT_PER_M = 3.00   # article's pricing assumption


def quarterly_cost(contracts: int = 10_000, avg_pages: int = 50, seg_tokens: int = 5000,
                   clauses: int = 3, tokens_per_extraction: int = 1000) -> Dict[str, float]:
    items = {
        "textract": contracts * avg_pages * TEXTRACT_PER_1000_PAGES / 1000,
        "claude_haiku_segmentation": contracts * seg_tokens * HAIKU_INPUT_PER_M / 1_000_000,
        "claude_sonnet_extraction": contracts * clauses * tokens_per_extraction * SONNET_INPUT_PER_M / 1_000_000,
        "lambda": 50.0,
        "fargate": 200.0,
        "step_functions": 10.0,
        "snowflake": 50.0,
        "cloudwatch": 20.0,
    }
    items["total"] = sum(items.values())
    items["per_contract"] = items["total"] / contracts
    return items


def tesseract_review_labor(contracts: int = 10_000, error_rate: float = 0.30,
                           minutes: float = 5, hourly: float = 50.0) -> float:
    """Tesseract-path review labor: contracts * error_rate * minutes at $/hr = $12,500 by default.

    NOTE: the article's later cost section states 25,000 minutes / ~$21,000, which does not
    follow from its own inputs (15,000 minutes). $12,500 matches its other sections.
    """
    return contracts * error_rate * minutes / 60 * hourly

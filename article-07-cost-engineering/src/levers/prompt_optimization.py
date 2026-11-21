"""Lever 3: Prompt optimization (article: 15-30% fewer tokens).

Tactics: terse instructions, structured JSON output, system prompt sent via the
system field (cacheable), 2-3 few-shot examples instead of 5, no chat history.
"""

import re
from typing import Dict, List, Optional

VERBOSE_PHRASES = [
    (r"please\s+", ""),
    (r"analyze the following (support )?ticket and respond with", "Respond with"),
    (r"classify the following document into one of these categories\.*", "Classify into:"),
    (r"provide a detailed explanation\.?", ""),
    (r"\bin order to\b", "to"),
    (r"\bkindly\b\s*", ""),
]


def estimate_tokens(text: str) -> int:
    """Cheap offline estimate (~4 chars/token). Real counts come from API usage."""
    return max(1, (len(text or "") + 3) // 4)


def strip_verbose(prompt: str) -> str:
    out = prompt
    for pat, rep in VERBOSE_PHRASES:
        out = re.sub(pat, rep, out, flags=re.I)
    return re.sub(r"[ \t]+", " ", out).strip()


def trim_few_shot(examples: List[dict], keep: int = 2) -> List[dict]:
    """Keep the best N examples (caller orders by quality; default 2 of 5)."""
    return examples[:keep]


def build_optimized_prompt(
    ticket_text: str,
    schema: str = "{sentiment: 'positive'|'neutral'|'negative', confidence: 0.0-1.0, "
                  "escalation_risk: 'low'|'medium'|'high'}",
    examples: Optional[List[Dict[str, str]]] = None,
    keep_examples: int = 2,
) -> Dict[str, str]:
    """Return {'system': stable prefix (cacheable), 'user': per-record text}."""
    examples = trim_few_shot(examples or [], keep_examples)
    shots = "\n".join(f"In: {e['input']}\nOut: {e['output']}" for e in examples)
    system = f"Respond with JSON only: {schema}"
    if shots:
        system += f"\n{shots}"
    return {"system": system, "user": ticket_text}


def token_reduction(original_tokens: int, optimized_tokens: int) -> float:
    """Fraction saved (article worked example: 400 -> 280 = 0.30)."""
    if original_tokens <= 0:
        return 0.0
    return round((original_tokens - optimized_tokens) / original_tokens, 4)

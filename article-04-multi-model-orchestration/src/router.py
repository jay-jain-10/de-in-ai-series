"""Model router: reads the YAML config and decides which model serves which task.

- sentiment: complexity score (word count, sentence length, keywords) > threshold -> fallback model
- aspect_extraction: top-k vector similarity >= threshold -> default RAG model, else fallback RAG model
- toxicity_detection: urgent safety keyword -> Opus, else Haiku

Also implements the Pattern 1 simple/complex classifier.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .config import RouterConfig


@dataclass
class RoutingDecision:
    task: str
    model: str
    reason: str  # e.g. "router_complexity_score=0.35, used_haiku_because < 0.6"
    signals: dict = field(default_factory=dict)


class AspectRetriever(Protocol):
    def search(self, text: str, k: int = 3) -> list:
        """Return [(aspect, similarity)] sorted best-first."""


class KeywordAspectRetriever:
    """Lexical stand-in for embedding + vector-store lookup (no network, deterministic).

    similarity = 0.70 + 0.15 * matched_keywords (capped at 1.0) when at least one keyword
    matches, else 0.0. Swap in a Voyage/OpenAI embedding + vector store in production.
    """

    DEFAULT_ASPECTS = {
        "battery": ["battery", "charge", "charging"],
        "software": ["software", "app", "firmware", "bug", "buggy"],
        "customer service": ["support", "service", "refund"],
        "price": ["price", "cost", "expensive", "cheap"],
        "build quality": ["build", "plastic", "sturdy", "broke", "screen"],
        "shipping": ["shipping", "delivery", "arrived"],
    }

    def __init__(self, aspects: Optional[dict] = None):
        self.aspects = aspects or self.DEFAULT_ASPECTS

    def search(self, text: str, k: int = 3) -> list:
        tokens = set(re.findall(r"[a-z']+", text.lower()))
        scored = []
        for aspect, keywords in self.aspects.items():
            matches = sum(1 for kw in keywords if kw in tokens)
            if matches:
                scored.append((aspect, min(1.0, 0.70 + 0.15 * matches)))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


def _words(text: str) -> list:
    return re.findall(r"\S+", text)


def contains_keyword(text: str, keywords) -> list:
    lowered = text.lower()
    return [k for k in keywords if re.search(rf"\b{re.escape(k.lower())}", lowered)]


class ModelRouter:
    def __init__(self, config: RouterConfig, retriever: Optional[AspectRetriever] = None):
        self.config = config
        self.retriever = retriever or KeywordAspectRetriever()

    # ---- signals ----------------------------------------------------
    def complexity_score(self, text: str) -> float:
        """0..1: 50% length (saturates at 300 words), 20% avg sentence length
        (saturates at 30 words), 30% complex-keyword presence."""
        words = _words(text)
        sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()] or [text]
        length = min(len(words) / 300, 1.0)
        sentence = min((len(words) / len(sentences)) / 30, 1.0)
        kw_list = self.config.patterns["router"]["complex_keywords"]
        keyword = 1.0 if contains_keyword(text, kw_list) else 0.0
        return round(0.5 * length + 0.2 * sentence + 0.3 * keyword, 4)

    # ---- per-task decisions -----------------------------------------
    def decide(self, task: str, text: str) -> RoutingDecision:
        route = self.config.tasks[task]
        if route.router == "complexity_score":
            score = self.complexity_score(text)
            if score > route.complexity_threshold:
                model, why = route.fallback_model, f"> {route.complexity_threshold}"
            else:
                model, why = route.default_model, f"<= {route.complexity_threshold}"
            return RoutingDecision(
                task, model,
                f"router_complexity_score={score}, used_{model}_because {why}",
                {"complexity_score": score},
            )
        if route.router == "embedding_similarity":
            hits = self.retriever.search(text)
            top = hits[0][1] if hits else 0.0
            if top >= route.embedding_threshold:
                model, why = route.default_model, f">= {route.embedding_threshold}"
            else:
                model, why = route.fallback_model, f"< {route.embedding_threshold}"
            return RoutingDecision(
                task, model,
                f"router_embedding_similarity={top:.2f}, used_{model}_because {why}",
                {"top_similarity": top, "retrieved": [a for a, _ in hits]},
            )
        if route.router == "keyword_urgency":
            found = contains_keyword(text, route.safety_keywords)
            if found:
                return RoutingDecision(
                    task, route.fallback_model,
                    f"router_keyword_urgency={found}, used_{route.fallback_model}_because urgent keyword",
                    {"keywords": found},
                )
            return RoutingDecision(
                task, route.default_model,
                f"router_keyword_urgency=[], used_{route.default_model}_because no urgent keyword",
                {"keywords": []},
            )
        raise ValueError(f"Unknown router type '{route.router}' for task {task}")

    # ---- Pattern 1 --------------------------------------------------
    def classify_complexity(self, text: str) -> str:
        """simple (<100 words, no keywords) / complex (>300 words or keyword).
        Reviews in between fall back to the complexity score threshold."""
        cfg = self.config.patterns["router"]
        n = len(_words(text))
        if contains_keyword(text, cfg["complex_keywords"]) or n > cfg["complex_min_words"]:
            return "complex"
        if n < cfg["simple_max_words"]:
            return "simple"
        threshold = self.config.tasks["sentiment"].complexity_threshold
        return "complex" if self.complexity_score(text) > threshold else "simple"

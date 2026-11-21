"""End-to-end lineage: every step logs timestamp, processing_version, model, tokens, cost, confidence."""

from collections import defaultdict
from typing import Optional

from .contracts import LineageStep
from .messaging import Publisher


class LineageRecorder:
    def __init__(self, processing_version: str, publisher: Optional[Publisher] = None,
                 topic: str = "platform.lineage"):
        self.processing_version = processing_version
        self.publisher, self.topic = publisher, topic
        self._steps: dict[str, list[LineageStep]] = defaultdict(list)

    def record(self, document_id: str, step: str, *, model_used: Optional[str] = None,
               token_count: int = 0, cost_usd: float = 0.0, confidence: Optional[float] = None,
               **details) -> LineageStep:
        s = LineageStep(document_id=document_id, step=step, processing_version=self.processing_version,
                        model_used=model_used, token_count=token_count, cost_usd=round(cost_usd, 8),
                        confidence_score=confidence, details=details)
        self._steps[document_id].append(s)
        if self.publisher:
            self.publisher.publish(self.topic, document_id, s.model_dump(mode="json"))
        return s

    def trace(self, document_id: str) -> list[LineageStep]:
        return list(self._steps.get(document_id, []))

    def totals(self, document_id: str) -> dict:
        steps = self.trace(document_id)
        return {"steps": len(steps), "tokens": sum(s.token_count for s in steps),
                "cost_usd": round(sum(s.cost_usd for s in steps), 8),
                "models": sorted({s.model_used for s in steps if s.model_used})}

    def explain(self, document_id: str) -> list[str]:
        """Human-readable root-cause trail ('why was this claim classified high-risk?')."""
        out = []
        for s in self.trace(document_id):
            bits = [f"{s.timestamp.isoformat()} {s.step}"]
            if s.model_used:
                bits.append(f"model={s.model_used} tokens={s.token_count} cost=${s.cost_usd:.6f}")
            if s.confidence_score is not None:
                bits.append(f"confidence={s.confidence_score:.2f}")
            if s.details:
                bits.append(str(s.details))
            out.append(" | ".join(bits))
        return out

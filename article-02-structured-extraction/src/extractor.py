"""Stage 4: Extraction with schema validation.

For each clause type, the most relevant chunks are sent to Claude Sonnet.
The reply must satisfy the ClauseExtraction Pydantic schema (value,
confidence, page_number, source_span). If it does not, we re-prompt with a
clearer example, up to max_extraction_attempts. Exhausted attempts raise
PipelineFailure('extraction') so the document goes to the DLQ.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.config import PipelineConfig
from src.llm_utils import UsageTracker, call_claude, parse_json
from src.models import Chunk, ClauseExtraction, ContractExtraction, ExtractedText, PipelineFailure
import json

logger = logging.getLogger(__name__)

CLAUSE_DESCRIPTIONS = {
    "payment_terms": "payment terms (e.g. net payment period such as '30 days')",
    "termination_conditions": "termination conditions (notice periods, termination for cause/convenience)",
    "liability_cap": "liability cap (maximum liability amount or formula)",
    "governing_law": "governing law / jurisdiction",
}

CLAUSE_KEYWORDS = {
    "payment_terms": ["payment", "invoice", "fees", "net ", "pay "],
    "termination_conditions": ["terminat", "notice", "expire", "cancel"],
    "liability_cap": ["liabil", "cap", "indemn", "damages"],
    "governing_law": ["governing law", "governed by", "jurisdiction", "laws of"],
}

EXTRACTION_PROMPT = """You are extracting the {clause_label} from a vendor contract excerpt.

Return a JSON object with exactly these fields:
- value: the extracted value as a short string, or null if the excerpt does not contain it. NEVER guess.
- confidence: float between 0.0 and 1.0
- page_number: integer page where the value was found (see [[PAGE n]] markers), or null
- source_span: the EXACT text copied verbatim from the excerpt that supports the value, or null

Example:
{{"value": "30 days", "confidence": 0.93, "page_number": 4, "source_span": "within thirty (30) days of invoice"}}

{retry_note}Excerpt:
{excerpt}

Return ONLY valid JSON. No markdown."""

RETRY_NOTE = (
    "IMPORTANT: your previous reply was rejected ({error}). Every field "
    "(value, confidence, page_number, source_span) MUST be present; "
    "confidence MUST be a number between 0.0 and 1.0.\n\n"
)


def select_chunks(chunks: List[Chunk], clause: str, limit: int = 3) -> List[Chunk]:
    """Keyword-score chunks (title counts double) and keep the top few."""
    kws = CLAUSE_KEYWORDS[clause]
    scored = []
    for c in chunks:
        body, title = c.text.lower(), c.title.lower()
        score = sum(body.count(k) for k in kws) + 5 * sum(k in title for k in kws)
        if score:
            scored.append((score, c))
    scored.sort(key=lambda t: (-t[0], t[1].chunk_id))
    return [c for _, c in scored[:limit]]


class ClauseExtractor:
    def __init__(self, client, config: PipelineConfig = None, tracker: Optional[UsageTracker] = None):
        self.client = client
        self.config = config or PipelineConfig()
        self.tracker = tracker or UsageTracker()
        self.attempt_counts: Dict[str, int] = {}

    def extract_clause(self, clause: str, chunks: List[Chunk]) -> ClauseExtraction:
        selected = select_chunks(chunks, clause)
        if not selected:
            return ClauseExtraction(value=None, confidence=0.0)  # nothing relevant found
        excerpt = "\n\n".join(f"## {c.title}\n{c.text}" for c in selected)

        retry_note, last_error = "", "unknown"
        for attempt in range(1, self.config.max_extraction_attempts + 1):
            self.attempt_counts[clause] = attempt
            prompt = EXTRACTION_PROMPT.format(
                clause_label=CLAUSE_DESCRIPTIONS[clause], retry_note=retry_note, excerpt=excerpt)
            try:
                raw = call_claude(
                    self.client, self.config.anthropic.extraction_model, prompt,
                    self.config.anthropic.extraction_max_tokens, self.tracker,
                    self.config.anthropic.max_retries)
                return ClauseExtraction(**parse_json(raw))
            except (json.JSONDecodeError, ValidationError, TypeError) as e:
                last_error = str(e).splitlines()[0][:200]
                logger.warning(f"{clause}: attempt {attempt} rejected: {last_error}")
                retry_note = RETRY_NOTE.format(error=last_error)
            except Exception as e:  # API errors after retries
                raise PipelineFailure("extraction", f"{clause}: API error: {e}",
                                      {"attempt_count": attempt})
        raise PipelineFailure(
            "extraction", f"{clause}: no schema-valid reply after "
            f"{self.config.max_extraction_attempts} attempts ({last_error})",
            {"attempt_count": self.config.max_extraction_attempts})

    def extract_contract(self, contract_id: str, chunks: List[Chunk], extracted: ExtractedText
                         ) -> Tuple[ContractExtraction, Dict[str, ClauseExtraction]]:
        clauses = {c: self.extract_clause(c, chunks) for c in self.config.clause_types}
        record = ContractExtraction(
            contract_id=contract_id,
            extraction_version=self.config.extraction_version,
            confidence_scores={k: v.confidence for k, v in clauses.items()},
            page_references={k: v.page_number for k, v in clauses.items()},
            source_spans={k: v.source_span for k, v in clauses.items()},
            extraction_method=extracted.extraction_method,
            ocr_confidence=extracted.confidence_score if extracted.extraction_method != "PyPDF2" else None,
            prompt_version=self.config.prompt_version,
            model_name=self.config.anthropic.extraction_model,
            processing_timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **{k: v.value for k, v in clauses.items()},
        )
        return record, clauses

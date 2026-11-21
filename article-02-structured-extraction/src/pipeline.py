"""End-to-end orchestration of the six stages for one contract.

Stage 1 detect -> 2 extract text -> 3 chunk -> 4 extract clauses ->
5 validate/route -> 6 load (or DLQ on hard failure).
This is what the Fargate task runs; Lambda handlers call the same pieces.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.chunker import ContractChunker
from src.config import PipelineConfig
from src.dlq import DeadLetterQueue, build_error_record
from src.extractor import ClauseExtractor
from src.format_detector import detect_format
from src.llm_utils import UsageTracker
from src.models import ContractExtraction, PipelineFailure, Route
from src.text_extractor import TextExtractor
from src.validator import ExtractionValidator, expect_median_confidence

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    contract_id: str
    status: str                      # loaded | quarantined | dlq
    route: Optional[str] = None
    record: Optional[ContractExtraction] = None
    failure_stage: Optional[str] = None
    error: Optional[str] = None
    elapsed_seconds: float = 0.0


class ContractPipeline:
    def __init__(self, client, text_extractor: TextExtractor, dlq: DeadLetterQueue,
                 loader, config: PipelineConfig = None):
        self.client = client
        self.config = config or PipelineConfig()
        self.text_extractor = text_extractor
        self.dlq = dlq
        self.loader = loader
        self.tracker = UsageTracker()
        self.validator = ExtractionValidator(self.config)

    def process_contract(self, contract_id: str, pdf_bytes: bytes, s3_key: str,
                         contract_date: Optional[str] = None, load: bool = True) -> PipelineResult:
        start = time.time()
        stage, attempts = "format_detection", 1
        try:
            meta = detect_format(contract_id, pdf_bytes, self.config)
            stage = "text_extraction"
            extracted = self.text_extractor.extract(
                pdf_bytes, meta, {"bucket": self.config.s3.bucket, "key": s3_key})
            stage = "chunking"
            chunks = ContractChunker(self.client, self.config, self.tracker).chunk(extracted)
            stage = "extraction"
            extractor = ClauseExtractor(self.client, self.config, self.tracker)
            record, clauses = extractor.extract_contract(contract_id, chunks, extracted)
            attempts = max(extractor.attempt_counts.values(), default=1)
            record = record.model_copy(update={"contract_date": contract_date})
            stage = "validation"
            result = self.validator.validate(record, clauses, extracted, meta.total_pages)
            if result.route == Route.DLQ:
                raise PipelineFailure("validation", "; ".join(result.flags) or "confidence 0",
                                      {"attempt_count": attempts})
            record = self.validator.apply(record, result)
        except PipelineFailure as f:
            return self._dead_letter(contract_id, s3_key, f.stage, f.message, f.details, attempts, start)
        except Exception as e:  # unexpected -> never lose the document
            return self._dead_letter(contract_id, s3_key, stage, f"Unexpected: {e}", {}, attempts, start)

        if load:
            self.loader.load([record])
        status = "quarantined" if record.routing == "quarantine" else "loaded"
        return PipelineResult(contract_id, status, record.routing, record,
                              elapsed_seconds=time.time() - start)

    def _dead_letter(self, contract_id, s3_key, stage, message, details, attempts, start):
        err = build_error_record(
            contract_id, s3_key, self.config.s3.bucket, stage, message,
            attempt_count=details.get("attempt_count", attempts),
            last_attempt_model=self.config.anthropic.extraction_model,
            ocr_confidence_scores=details.get("ocr_confidence_scores"))
        self.dlq.send(err, s3_key)
        return PipelineResult(contract_id, "dlq", Route.DLQ.value, None, stage, message,
                              time.time() - start)

    def process_batch(self, items: List[Dict]) -> Dict:
        """items: dicts with contract_id, pdf_bytes, s3_key[, contract_date]. Loads in one MERGE."""
        results = [self.process_contract(i["contract_id"], i["pdf_bytes"], i["s3_key"],
                                         i.get("contract_date"), load=False) for i in items]
        records = [r.record for r in results if r.record]
        if records:
            self.loader.load(records)
        confs = [c for r in records for c in r.confidence_scores.values() if c > 0]
        return {
            "results": results,
            "loaded": sum(r.status == "loaded" for r in results),
            "quarantined": sum(r.status == "quarantined" for r in results),
            "dlq": sum(r.status == "dlq" for r in results),
            "median_confidence_ok": expect_median_confidence(confs, self.config.median_confidence_floor)
            if confs else None,
        }

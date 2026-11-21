"""Stage 6: Dead-letter queue (S3 quarantine prefix) + daily summary.

Hard failures land under s3://bucket/quarantine/<contract_id>/ with the
original PDF and an error.json carrying full failure context.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.config import PipelineConfig

logger = logging.getLogger(__name__)


def build_error_record(contract_id: str, s3_key: str, bucket: str, failure_stage: str,
                       error_message: str, attempt_count: int = 1,
                       last_attempt_model: Optional[str] = None,
                       ocr_confidence_scores: Optional[List[float]] = None,
                       now: Optional[datetime] = None) -> Dict:
    now = now or datetime.now(timezone.utc)
    record = {
        "contract_id": contract_id,
        "s3_key": f"s3://{bucket}/{s3_key}",
        "failure_stage": failure_stage,
        "error_message": error_message,
        "processing_timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempt_count": attempt_count,
        "last_attempt_model": last_attempt_model,
    }
    if ocr_confidence_scores is not None:
        record["ocr_confidence_scores"] = ocr_confidence_scores
    return record


class DeadLetterQueue:
    def __init__(self, s3_client, config: PipelineConfig = None):
        self.s3 = s3_client
        self.config = config or PipelineConfig()
        self.bucket = self.config.s3.bucket
        self.prefix = self.config.s3.quarantine_prefix

    def send(self, error_record: Dict, source_key: Optional[str] = None) -> str:
        """Write error metadata (and copy the source PDF) to quarantine."""
        cid = error_record["contract_id"]
        err_key = f"{self.prefix}{cid}/error.json"
        self.s3.put_object(Bucket=self.bucket, Key=err_key,
                           Body=json.dumps(error_record).encode(), ContentType="application/json")
        if source_key:
            self.s3.copy_object(
                Bucket=self.bucket, Key=f"{self.prefix}{cid}/{source_key.rsplit('/', 1)[-1]}",
                CopySource={"Bucket": self.bucket, "Key": source_key})
        logger.warning(f"Quarantined {cid} at stage {error_record['failure_stage']}")
        return err_key

    def list_errors(self) -> List[Dict]:
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                if obj["Key"].endswith("/error.json"):
                    body = self.s3.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read()
                    out.append(json.loads(body))
            if not resp.get("IsTruncated"):
                return out
            token = resp.get("NextContinuationToken")


def summarize_quarantine(errors: List[Dict]) -> Dict:
    """Daily summary used by the ticket-creating Lambda."""
    by_stage = Counter(e["failure_stage"] for e in errors)
    return {
        "total": len(errors),
        "by_stage": dict(by_stage),
        "tickets": [
            {"title": f"Manual review: {e['contract_id']} ({e['failure_stage']})",
             "contract_id": e["contract_id"], "error": e["error_message"]}
            for e in errors
        ],
    }

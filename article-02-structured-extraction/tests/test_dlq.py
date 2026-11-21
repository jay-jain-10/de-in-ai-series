import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.dlq import DeadLetterQueue, build_error_record, summarize_quarantine


def rec(**kw):
    base = dict(contract_id="vendor-acme-2024-001", s3_key="contracts/vendor-acme-2024-001.pdf",
                bucket="test-bucket", failure_stage="ocr", error_message="Textract confidence < 0.5 on all pages",
                attempt_count=3, last_attempt_model="m", ocr_confidence_scores=[0.42, 0.38, 0.51, 0.45],
                now=datetime(2024, 3, 12, 14, 32, 15, tzinfo=timezone.utc))
    base.update(kw)
    return build_error_record(**base)


def test_error_record_matches_article_shape():
    r = rec()
    assert r == {
        "contract_id": "vendor-acme-2024-001",
        "s3_key": "s3://test-bucket/contracts/vendor-acme-2024-001.pdf",
        "failure_stage": "ocr",
        "error_message": "Textract confidence < 0.5 on all pages",
        "ocr_confidence_scores": [0.42, 0.38, 0.51, 0.45],
        "processing_timestamp": "2024-03-12T14:32:15Z",
        "attempt_count": 3,
        "last_attempt_model": "m",
    }


def test_send_writes_error_json_and_copies_pdf(fake_s3, config):
    key = DeadLetterQueue(fake_s3, config).send(rec(), "contracts/vendor-acme-2024-001.pdf")
    assert key == "quarantine/vendor-acme-2024-001/error.json"
    assert json.loads(fake_s3.store[key])["failure_stage"] == "ocr"
    kw = fake_s3.copy_object.call_args.kwargs
    assert kw["Key"] == "quarantine/vendor-acme-2024-001/vendor-acme-2024-001.pdf"
    assert kw["CopySource"]["Key"] == "contracts/vendor-acme-2024-001.pdf"


def test_list_errors_paginates(config):
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = [
        {"Contents": [{"Key": "quarantine/a/error.json"}, {"Key": "quarantine/a/a.pdf"}],
         "IsTruncated": True, "NextContinuationToken": "t"},
        {"Contents": [{"Key": "quarantine/b/error.json"}], "IsTruncated": False}]
    s3.get_object.side_effect = lambda Bucket, Key: {"Body": MagicMock(read=lambda: json.dumps(
        {"contract_id": Key.split("/")[1], "failure_stage": "ocr", "error_message": "x"}).encode())}
    errs = DeadLetterQueue(s3, config).list_errors()
    assert [e["contract_id"] for e in errs] == ["a", "b"]
    assert s3.list_objects_v2.call_args.kwargs["ContinuationToken"] == "t"


def test_summary_counts_by_stage_and_creates_tickets():
    s = summarize_quarantine([{"contract_id": "a", "failure_stage": "ocr", "error_message": "x"},
                              {"contract_id": "b", "failure_stage": "ocr", "error_message": "y"},
                              {"contract_id": "c", "failure_stage": "extraction", "error_message": "z"}])
    assert s["total"] == 3 and s["by_stage"] == {"ocr": 2, "extraction": 1} and len(s["tickets"]) == 3

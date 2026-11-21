import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import cost_model, sla
from src.lambda_handlers import dead_letter_handler, daily_quarantine_summary_handler, detect_format_handler
from tests.conftest import PAY_TEXT, make_pdf

ASL = json.loads((Path(__file__).parent.parent / "step_functions" / "state_machine.json").read_text())


# -- state machine -----------------------------------------------------------------

def _targets(state):
    t = []
    if "Next" in state:
        t.append(state["Next"])
    t += [c["Next"] for c in state.get("Choices", [])]
    if "Default" in state:
        t.append(state["Default"])
    t += [c["Next"] for c in state.get("Catch", [])]
    return t


def test_state_machine_references_resolve():
    states = ASL["States"]
    assert ASL["StartAt"] in states
    for name, st in states.items():
        for t in _targets(st):
            assert t in states, f"{name} -> {t}"
        assert st.get("End") or _targets(st), f"{name} is a dead end"


def test_every_task_has_catch_to_dlq_or_fallback():
    for name, st in ASL["States"].items():
        if st["Type"] == "Task" and name != "SendToDLQ":
            assert st.get("Catch"), name


def test_all_states_reachable():
    seen, stack = set(), [ASL["StartAt"]]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack += _targets(ASL["States"][n])
    assert seen == set(ASL["States"])


def test_fargate_for_long_jobs_and_textract_native():
    s = ASL["States"]
    assert s["ChunkAndExtractFargate"]["Resource"].endswith("ecs:runTask.sync")
    assert s["ChunkAndExtractFargate"]["TimeoutSeconds"] == 3600
    assert s["ChunkAndExtractLambda"]["TimeoutSeconds"] == 900  # Lambda's 15 minute limit
    assert "textract" in s["StartTextract"]["Resource"]
    assert {c["StringEquals"] for c in s["RouteByDocType"]["Choices"]} == {"digital", "scanned", "hybrid"}


# -- lambda handlers ---------------------------------------------------------------

def test_detect_format_handler(config):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: make_pdf([PAY_TEXT, None]))}
    out = detect_format_handler({"bucket": "b", "key": "k.pdf", "contract_id": "c"}, deps={"s3": s3, "config": config})
    assert out["doc_type"] == "hybrid" and out["total_pages"] == 2 and out["use_fargate"] is False


def test_large_document_selects_fargate(config):
    config.fargate_page_threshold = 2
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: make_pdf([PAY_TEXT] * 3))}
    out = detect_format_handler({"bucket": "b", "key": "k", "contract_id": "c"}, deps={"s3": s3, "config": config})
    assert out["use_fargate"] is True


def test_dead_letter_handler_parses_step_functions_error(config, fake_s3):
    event = {"bucket": "test-bucket", "key": "contracts/x.pdf", "contract_id": "x",
             "error": {"Error": "States.TaskFailed", "Cause": json.dumps({"errorMessage": "Textract blew up"})}}
    out = dead_letter_handler(event, deps={"s3": fake_s3, "config": config})
    assert out["dlq_record"]["error_message"] == "Textract blew up"
    assert "quarantine/x/error.json" in fake_s3.store


def test_daily_summary_handler(config):
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "quarantine/a/error.json"}], "IsTruncated": False}
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(
        {"contract_id": "a", "failure_stage": "ocr", "error_message": "e"}).encode())}
    out = daily_quarantine_summary_handler({}, deps={"s3": s3, "config": config})
    assert out["total"] == 1 and out["by_stage"] == {"ocr": 1}


# -- SLAs --------------------------------------------------------------------------

def test_extraction_sla_p95():
    ok = [timedelta(minutes=10)] * 96 + [timedelta(hours=6)] * 4
    assert not sla.check_extraction_sla(ok)["breached"]
    bad = [timedelta(minutes=10)] * 90 + [timedelta(hours=6)] * 10
    r = sla.check_extraction_sla(bad)
    assert r["breached"] and r["action"] == "alert ops team"


def test_review_sla_24h():
    t0 = datetime(2026, 1, 1)
    assert not sla.check_review_sla(t0, t0 + timedelta(hours=23))["breached"]
    r = sla.check_review_sla(t0, now=t0 + timedelta(hours=25))
    assert r["breached"] and "VP of Product" in r["action"]


def test_review_sla_defaults_to_now_for_open_items():
    from datetime import timezone
    open_item = datetime.now(timezone.utc) - timedelta(hours=30)
    assert sla.check_review_sla(open_item)["breached"]
    assert not sla.check_review_sla(datetime.now() - timedelta(hours=1))["breached"]


def test_feedback_sla_one_week():
    t0 = datetime(2026, 1, 1)
    assert not sla.check_feedback_sla(t0, t0 + timedelta(days=6))["breached"]
    assert sla.check_feedback_sla(t0, t0 + timedelta(days=8))["breached"]


def test_review_resolution_rate():
    t0 = datetime(2026, 1, 1)
    ev = [{"quarantined_at": t0, "resolved_at": t0 + timedelta(hours=3)},
          {"quarantined_at": t0, "resolved_at": t0 + timedelta(hours=30)},
          {"quarantined_at": t0, "resolved_at": None}]
    assert sla.review_resolution_rate(ev) == pytest.approx(1 / 3)


# -- cost model --------------------------------------------------------------------

def test_cost_model_matches_article():
    c = cost_model.quarterly_cost()
    assert c["textract"] == 750 and c["claude_haiku_segmentation"] == 40 and c["claude_sonnet_extraction"] == 90
    assert c["total"] == 1210 and c["per_contract"] == pytest.approx(0.121)


def test_tesseract_labor_arithmetic():
    # 10,000 x 30% x 5 min = 15,000 min = 250 h x $50 = $12,500 (matches the article's
    # problem statement and Textract-vs-Tesseract section). The article's cost-analysis
    # section says "25,000 minutes ... ~$21,000", which is arithmetically inconsistent.
    assert cost_model.tesseract_review_labor() == pytest.approx(12_500)

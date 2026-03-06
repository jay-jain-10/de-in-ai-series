"""
Unit tests for the batch processor.

Tests cover:
- Batch processing with mocked classifier
- Empty input handling
- Error resilience
"""

import pytest
from unittest.mock import MagicMock, patch
from src.batch_processor import BatchProcessor
from src.classifier import TicketClassification, Sentiment, Category, EscalationRisk


@pytest.fixture
def mock_classifier():
    classifier = MagicMock()
    classifier.model = "claude-haiku-4-5-20251001"
    classifier.classify_ticket.return_value = TicketClassification(
        sentiment=Sentiment.NEGATIVE,
        category=Category.BILLING,
        escalation_risk=EscalationRisk.MEDIUM,
        confidence=0.88,
        reasoning="Test classification",
    )
    classifier.get_cost_estimate.return_value = {
        "input_tokens": 500,
        "output_tokens": 160,
        "total_requests": 2,
        "total_failures": 0,
        "failure_rate": 0.0,
        "estimated_cost_usd": 0.0013,
    }
    return classifier


@pytest.fixture
def sample_tickets():
    return [
        {
            "ticket_id": "TKT-001",
            "created_at": "2026-02-15T14:32:00Z",
            "customer_id": "CUST-001",
            "channel": "email",
            "subject": "Billing issue",
            "body": "I was charged twice.",
        },
        {
            "ticket_id": "TKT-002",
            "created_at": "2026-02-15T15:00:00Z",
            "customer_id": "CUST-002",
            "channel": "chat",
            "subject": "Login problem",
            "body": "Can't access my account.",
        },
    ]


class TestBatchProcessor:
    def test_process_tickets_returns_all_results(
        self, mock_classifier, sample_tickets
    ):
        processor = BatchProcessor(mock_classifier, max_workers=2)
        results = processor.process_tickets(sample_tickets)

        assert len(results) == 2
        assert all(r["sentiment"] == "negative" for r in results)
        assert all(r["model_used"] == "claude-haiku-4-5-20251001" for r in results)

    def test_process_empty_list(self, mock_classifier):
        processor = BatchProcessor(mock_classifier)
        results = processor.process_tickets([])
        assert results == []

    def test_result_has_required_fields(self, mock_classifier, sample_tickets):
        processor = BatchProcessor(mock_classifier, max_workers=1)
        results = processor.process_tickets(sample_tickets)

        required_fields = [
            "ticket_id", "created_at", "customer_id", "channel",
            "subject", "sentiment", "category", "escalation_risk",
            "confidence", "ai_reasoning", "model_used", "classified_at",
        ]
        for result in results:
            for field in required_fields:
                assert field in result, f"Missing field: {field}"

    def test_classified_at_is_populated(self, mock_classifier, sample_tickets):
        processor = BatchProcessor(mock_classifier, max_workers=1)
        results = processor.process_tickets(sample_tickets)

        for result in results:
            assert result["classified_at"] is not None

    def test_handles_classifier_exception(self, sample_tickets):
        """Processor should handle exceptions from individual ticket classifications."""
        failing_classifier = MagicMock()
        failing_classifier.model = "claude-haiku-4-5-20251001"
        failing_classifier.classify_ticket.side_effect = Exception("API boom")
        failing_classifier.get_cost_estimate.return_value = {
            "input_tokens": 0, "output_tokens": 0,
            "total_requests": 0, "total_failures": 0,
            "failure_rate": 0.0, "estimated_cost_usd": 0.0,
        }

        processor = BatchProcessor(failing_classifier, max_workers=1)
        results = processor.process_tickets(sample_tickets)

        # All should fail, results empty
        assert len(results) == 0

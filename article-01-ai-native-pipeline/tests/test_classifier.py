"""
Unit tests for the ticket classifier.

Tests cover:
- Pydantic schema validation
- Prompt template rendering
- Fallback classification behavior
- Cost estimation math
"""

import pytest
import json
from unittest.mock import MagicMock, patch
from src.classifier import (
    TicketClassifier,
    TicketClassification,
    Sentiment,
    Category,
    EscalationRisk,
    CLASSIFICATION_PROMPT,
)


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def sample_ticket():
    return {
        "ticket_id": "TKT-TEST-001",
        "subject": "Refund not processed",
        "body": "I've been waiting 3 weeks for my refund. This is unacceptable.",
        "channel": "email",
        "created_at": "2026-02-15T14:32:00Z",
        "customer_id": "CUST-001",
    }


@pytest.fixture
def valid_api_response():
    return {
        "sentiment": "frustrated",
        "category": "billing",
        "escalation_risk": "high",
        "confidence": 0.92,
        "reasoning": "Customer is frustrated about delayed refund.",
    }


@pytest.fixture
def classifier():
    return TicketClassifier(api_key="test-key")


# ─── Schema Tests ─────────────────────────────────────────────────────

class TestTicketClassificationSchema:
    def test_valid_classification(self, valid_api_response):
        result = TicketClassification(**valid_api_response)
        assert result.sentiment == Sentiment.FRUSTRATED
        assert result.category == Category.BILLING
        assert result.escalation_risk == EscalationRisk.HIGH
        assert result.confidence == 0.92

    def test_invalid_sentiment_rejected(self, valid_api_response):
        valid_api_response["sentiment"] = "angry"
        with pytest.raises(ValueError):
            TicketClassification(**valid_api_response)

    def test_confidence_out_of_range(self, valid_api_response):
        valid_api_response["confidence"] = 1.5
        with pytest.raises(ValueError):
            TicketClassification(**valid_api_response)

    def test_confidence_negative_rejected(self, valid_api_response):
        valid_api_response["confidence"] = -0.1
        with pytest.raises(ValueError):
            TicketClassification(**valid_api_response)

    def test_all_sentiment_values(self):
        for s in ["positive", "negative", "neutral", "frustrated"]:
            assert Sentiment(s).value == s

    def test_all_category_values(self):
        for c in ["billing", "technical", "account", "product", "general", "compliance"]:
            assert Category(c).value == c


# ─── Prompt Tests ─────────────────────────────────────────────────────

class TestPromptTemplate:
    def test_prompt_renders_with_ticket_data(self, sample_ticket):
        rendered = CLASSIFICATION_PROMPT.format(
            subject=sample_ticket["subject"],
            body=sample_ticket["body"],
            channel=sample_ticket["channel"],
        )
        assert "Refund not processed" in rendered
        assert "3 weeks" in rendered
        assert "email" in rendered

    def test_prompt_handles_empty_fields(self):
        rendered = CLASSIFICATION_PROMPT.format(
            subject="", body="", channel="unknown"
        )
        assert "unknown" in rendered


# ─── Classifier Tests ─────────────────────────────────────────────────

class TestTicketClassifier:
    @patch("src.classifier.anthropic.Anthropic")
    def test_successful_classification(
        self, mock_anthropic_class, classifier, sample_ticket, valid_api_response
    ):
        # Mock API response
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(valid_api_response))]
        mock_response.usage.input_tokens = 250
        mock_response.usage.output_tokens = 80

        classifier.client = MagicMock()
        classifier.client.messages.create.return_value = mock_response

        result = classifier.classify_ticket(sample_ticket)

        assert result.sentiment == Sentiment.FRUSTRATED
        assert result.category == Category.BILLING
        assert result.confidence == 0.92

    def test_fallback_on_failure(self, classifier, sample_ticket):
        result = classifier._fallback_classification(
            sample_ticket, "Test error"
        )
        assert result.confidence == 0.0
        assert result.sentiment == Sentiment.NEUTRAL
        assert result.escalation_risk == EscalationRisk.MEDIUM
        assert "FALLBACK" in result.reasoning

    def test_cost_estimation(self, classifier):
        classifier.total_input_tokens = 1_000_000
        classifier.total_output_tokens = 200_000
        classifier._request_count = 100
        classifier._failure_count = 2

        cost = classifier.get_cost_estimate()

        assert cost["input_tokens"] == 1_000_000
        assert cost["output_tokens"] == 200_000
        assert cost["estimated_cost_usd"] == 2.0  # $1 input + $1 output
        assert cost["failure_rate"] == 0.02

    def test_cost_estimation_zero_requests(self, classifier):
        cost = classifier.get_cost_estimate()
        assert cost["estimated_cost_usd"] == 0.0
        assert cost["failure_rate"] == 0.0

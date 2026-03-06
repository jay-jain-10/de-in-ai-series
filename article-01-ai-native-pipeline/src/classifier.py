"""
AI-powered ticket classifier using Claude Haiku.

Treats the LLM as a pipeline transformation stage with retry logic,
schema validation, graceful degradation, and cost tracking.
"""

import anthropic
import json
import time
import logging
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Schema Definitions ───────────────────────────────────────────────

class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    FRUSTRATED = "frustrated"


class Category(str, Enum):
    BILLING = "billing"
    TECHNICAL = "technical"
    ACCOUNT = "account"
    PRODUCT = "product"
    GENERAL = "general"
    COMPLIANCE = "compliance"


class EscalationRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TicketClassification(BaseModel):
    """Validated output schema for AI classification.

    Acts as the data contract between the AI stage and Snowflake.
    """
    sentiment: Sentiment
    category: Category
    escalation_risk: EscalationRisk
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


# ─── Prompt Template ──────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """You are a support ticket classifier for a fintech company.

Analyze the following customer support ticket and return a JSON object with these fields:
- sentiment: one of [positive, negative, neutral, frustrated]
- category: one of [billing, technical, account, product, general, compliance]
- escalation_risk: one of [low, medium, high, critical]
- confidence: float between 0.0 and 1.0
- reasoning: brief explanation of your classification (max 100 words)

Classification Rules:
- "frustrated" sentiment = customer expressing anger, threatening to leave, or demanding escalation
- "critical" escalation = mentions of legal action, regulatory complaints, media threats, or safety issues
- "high" escalation = requests for manager, repeated contacts (3+), or unresolved issues > 7 days
- "medium" escalation = general complaints with moderate urgency
- "low" escalation = simple questions, positive feedback, routine requests

Few-shot examples:

Example 1:
Subject: Can't log in to my account
Body: I've tried resetting my password 4 times and it still doesn't work. I have a payment due tomorrow.
Output: {{"sentiment": "frustrated", "category": "technical", "escalation_risk": "high", "confidence": 0.92, "reasoning": "Customer is frustrated by repeated login failures with time-sensitive payment deadline."}}

Example 2:
Subject: Thanks for the quick help!
Body: The agent resolved my issue in under 5 minutes. Great service.
Output: {{"sentiment": "positive", "category": "general", "escalation_risk": "low", "confidence": 0.97, "reasoning": "Positive feedback about support experience."}}

Now classify this ticket:

Ticket Subject: {subject}
Ticket Body: {body}
Channel: {channel}

Return ONLY valid JSON. No markdown, no explanation outside the JSON."""


# ─── Classifier ───────────────────────────────────────────────────────

class TicketClassifier:
    """Classifies support tickets using Claude Haiku with production safeguards.

    Features:
        - Retry with exponential backoff for rate limits
        - Pydantic validation of AI output
        - Graceful degradation on failure
        - Token usage tracking for cost monitoring
    """

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._request_count = 0
        self._failure_count = 0

    def classify_ticket(
        self, ticket: dict, max_retries: int = 3
    ) -> TicketClassification:
        """Classify a single ticket with retry logic.

        Args:
            ticket: Dict with keys: subject, body, channel
            max_retries: Number of retry attempts on failure

        Returns:
            TicketClassification with validated fields
        """
        prompt = CLASSIFICATION_PROMPT.format(
            subject=ticket.get("subject", ""),
            body=ticket.get("body", ""),
            channel=ticket.get("channel", "unknown"),
        )

        for attempt in range(max_retries):
            try:
                self._request_count += 1
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}],
                )

                # Track token usage
                self.total_input_tokens += response.usage.input_tokens
                self.total_output_tokens += response.usage.output_tokens

                # Parse and validate
                raw_text = response.content[0].text.strip()

                # Handle potential markdown wrapping
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

                parsed = json.loads(raw_text)
                classification = TicketClassification(**parsed)

                logger.debug(
                    f"Classified {ticket.get('ticket_id', 'unknown')}: "
                    f"sentiment={classification.sentiment.value}, "
                    f"confidence={classification.confidence}"
                )
                return classification

            except json.JSONDecodeError as e:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries}: "
                    f"Invalid JSON from API for ticket "
                    f"{ticket.get('ticket_id', 'unknown')}: {e}"
                )
                if attempt == max_retries - 1:
                    self._failure_count += 1
                    return self._fallback_classification(ticket, f"JSON parse error: {e}")

            except anthropic.RateLimitError:
                wait_time = 2 ** (attempt + 1)
                logger.warning(f"Rate limited. Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
                if attempt == max_retries - 1:
                    self._failure_count += 1
                    return self._fallback_classification(ticket, "Rate limit exceeded")

            except anthropic.APIError as e:
                logger.error(f"Anthropic API error: {e}")
                if attempt == max_retries - 1:
                    self._failure_count += 1
                    return self._fallback_classification(ticket, f"API error: {e}")

            except Exception as e:
                logger.error(f"Unexpected error classifying ticket: {e}")
                self._failure_count += 1
                return self._fallback_classification(ticket, str(e))

    def _fallback_classification(
        self, ticket: dict, error: str
    ) -> TicketClassification:
        """Return a safe default classification when AI fails.

        Sets confidence=0.0 so downstream dbt models can filter these
        into a human review queue.
        """
        logger.warning(
            f"Using fallback for ticket {ticket.get('ticket_id', 'unknown')}: {error}"
        )
        return TicketClassification(
            sentiment=Sentiment.NEUTRAL,
            category=Category.GENERAL,
            escalation_risk=EscalationRisk.MEDIUM,
            confidence=0.0,
            reasoning=f"FALLBACK: Classification failed - {error}",
        )

    def get_cost_estimate(self) -> dict:
        """Calculate estimated API costs based on Haiku pricing.

        Pricing (as of 2026):
            - Input:  $1.00 per 1M tokens
            - Output: $5.00 per 1M tokens
        """
        input_cost = (self.total_input_tokens / 1_000_000) * 1.00
        output_cost = (self.total_output_tokens / 1_000_000) * 5.00
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_requests": self._request_count,
            "total_failures": self._failure_count,
            "failure_rate": (
                round(self._failure_count / max(self._request_count, 1), 4)
            ),
            "estimated_cost_usd": round(input_cost + output_cost, 4),
        }

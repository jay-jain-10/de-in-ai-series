"""
Batch processor for concurrent ticket classification.

Handles parallel API calls with controlled concurrency to stay
within rate limits while maximizing throughput.
"""

import json
import boto3
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional

from .classifier import TicketClassifier

logger = logging.getLogger(__name__)


class BatchProcessor:
    """Process tickets in concurrent batches with progress tracking.

    Args:
        classifier: Configured TicketClassifier instance
        max_workers: Max concurrent API calls (tune based on API tier)
    """

    def __init__(self, classifier: TicketClassifier, max_workers: int = 5):
        self.classifier = classifier
        self.max_workers = max_workers

    def process_tickets(self, tickets: List[dict]) -> List[dict]:
        """Classify a batch of tickets concurrently.

        Args:
            tickets: List of ticket dicts with required keys

        Returns:
            List of enriched ticket dicts with classification columns
        """
        if not tickets:
            logger.warning("No tickets to process.")
            return []

        results = []
        failed = []
        total = len(tickets)

        logger.info(
            f"Starting classification of {total} tickets "
            f"with {self.max_workers} workers..."
        )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_ticket = {
                executor.submit(self._classify_single, ticket): ticket
                for ticket in tickets
            }

            completed = 0
            for future in as_completed(future_to_ticket):
                ticket = future_to_ticket[future]
                completed += 1

                try:
                    result = future.result()
                    results.append(result)

                    if completed % 100 == 0 or completed == total:
                        logger.info(f"Progress: {completed}/{total} tickets processed")

                except Exception as e:
                    logger.error(
                        f"Failed to classify {ticket.get('ticket_id', '?')}: {e}"
                    )
                    failed.append({
                        "ticket_id": ticket.get("ticket_id"),
                        "error": str(e),
                    })

        # Summary
        cost = self.classifier.get_cost_estimate()
        logger.info(
            f"Batch complete: {len(results)}/{total} succeeded, "
            f"{len(failed)} failed | "
            f"Cost: ${cost['estimated_cost_usd']} | "
            f"Failure rate: {cost['failure_rate']:.2%}"
        )

        return results

    def _classify_single(self, ticket: dict) -> dict:
        """Classify one ticket and merge AI output with source fields."""
        classification = self.classifier.classify_ticket(ticket)

        return {
            # Source fields (pass-through)
            "ticket_id": ticket["ticket_id"],
            "created_at": ticket.get("created_at"),
            "customer_id": ticket.get("customer_id"),
            "channel": ticket.get("channel"),
            "subject": ticket.get("subject"),
            # AI-generated columns
            "sentiment": classification.sentiment.value,
            "category": classification.category.value,
            "escalation_risk": classification.escalation_risk.value,
            "confidence": classification.confidence,
            "ai_reasoning": classification.reasoning,
            # Pipeline metadata
            "model_used": self.classifier.model,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }


def load_tickets_from_s3(
    bucket: str, prefix: str, region: Optional[str] = None
) -> List[dict]:
    """Load ticket JSON files from an S3 prefix.

    Expects JSON files with structure: {"tickets": [...]}

    Args:
        bucket: S3 bucket name
        prefix: S3 key prefix (e.g., "support-tickets/2026-03-01/")
        region: AWS region (optional)

    Returns:
        Flat list of ticket dicts
    """
    s3_kwargs = {}
    if region:
        s3_kwargs["region_name"] = region

    s3 = boto3.client("s3", **s3_kwargs)
    tickets = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue

            try:
                response = s3.get_object(Bucket=bucket, Key=key)
                data = json.loads(response["Body"].read().decode("utf-8"))
                batch = data.get("tickets", [])
                tickets.extend(batch)
                logger.debug(f"Loaded {len(batch)} tickets from s3://{bucket}/{key}")
            except Exception as e:
                logger.error(f"Failed to load s3://{bucket}/{key}: {e}")

    logger.info(f"Total: {len(tickets)} tickets from s3://{bucket}/{prefix}")
    return tickets

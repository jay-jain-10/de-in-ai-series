"""Lambda entry points called by Step Functions (Part 2).
Each handler can be packaged as its own Lambda; shown together for readability."""
from .schemas import InvoiceData
from .textract_extractor import extract_text_from_pdf, parse_textract_response
from .llm_extractor import classify_document, extract_invoice_data
from .validator import validate_and_store, REVIEW_TOPIC_ARN
import json
import logging
import boto3
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sns = boto3.client("sns", region_name="us-east-1")


def extraction_handler(event, context):
    """Lambda handler for Textract extraction."""
    bucket = event["bucket"]
    key = event["key"]

    logger.info(f"Extracting text from s3://{bucket}/{key}")

    response = extract_text_from_pdf(bucket, key)
    parsed = parse_textract_response(response)

    return {
        "bucket": bucket,
        "key": key,
        "full_text": parsed["full_text"],
        "tables": parsed["tables"],
        "forms": parsed["forms"],
        "textract_confidence": parsed["confidence"],
        "page_count": parsed["page_count"],
    }


def classification_handler(event, context):
    """Lambda handler for document classification."""
    text = event["full_text"]

    result = classify_document(text)

    return {
        **event,  # Pass through all previous data
        "document_type": result["document_type"],
        "classification_confidence": result["confidence"],
    }


def invoice_extraction_handler(event, context):
    """Lambda handler for structured invoice extraction."""
    invoice = extract_invoice_data(
        text=event["full_text"],
        tables=event["tables"],
        forms=event["forms"],
        textract_confidence=event["textract_confidence"],
    )

    return {
        "bucket": event["bucket"],
        "key": event["key"],
        "invoice": invoice.model_dump(mode="json"),
    }


def validation_handler(event, context):
    """Lambda handler for validation and storage."""
    invoice_data = event["invoice"]
    invoice = InvoiceData(**invoice_data)

    result = validate_and_store(invoice, s3_key=event["key"])

    logger.info(
        f"Invoice {result['invoice_id']}: {result['status']} "
        f"({len(result.get('issues', []))} issues)"
    )

    return result


def failure_handler(event, context):
    """Handle extraction failures — dead letter to S3 + alert."""
    logger.error(f"Pipeline failure: {json.dumps(event)}")

    s3 = boto3.client("s3")
    s3.copy_object(
        Bucket="invoices-dead-letter",
        CopySource={
            "Bucket": event.get("bucket", "unknown"),
            "Key": event.get("key", "unknown"),
        },
        Key=f"failed/{event.get('key', 'unknown')}",
    )

    sns.publish(
        TopicArn=REVIEW_TOPIC_ARN,
        Subject="Invoice Pipeline Failure",
        Message=f"Failed to process: {event.get('key')}\nError: {event.get('Error', 'Unknown')}",
    )

    return {"status": "failed", "moved_to": "dead-letter"}

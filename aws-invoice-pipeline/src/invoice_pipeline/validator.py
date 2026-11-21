"""Stage 3: validation, duplicate detection, storage, alerting (Part 2)."""
from .schemas import InvoiceData
import boto3
from datetime import date, timedelta
from decimal import Decimal


from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
sns = boto3.client("sns", region_name="us-east-1")

REVIEW_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789:invoice-review"
TABLE_NAME = "processed-invoices"


def validate_and_store(invoice: InvoiceData, s3_key: str) -> dict:
    """
    Validate extracted invoice data and either store it
    or flag it for human review.
    """
    issues = []

    # Check 1: Confidence threshold
    if invoice.confidence < 0.75:
        issues.append(
            f"Low confidence: {invoice.confidence:.2f}"
        )

    # Check 2: Invoice date sanity
    today = date.today()
    if invoice.invoice_date > today:
        issues.append(
            f"Future invoice date: {invoice.invoice_date}"
        )
    if invoice.invoice_date < today - timedelta(days=365):
        issues.append(
            f"Invoice older than 1 year: {invoice.invoice_date}"
        )

    # Check 3: Amount sanity
    if invoice.total_amount <= 0:
        issues.append(
            f"Non-positive total: {invoice.total_amount}"
        )
    if invoice.total_amount > 100_000:
        issues.append(
            f"Unusually large amount: ${invoice.total_amount:,.2f}"
        )

    # Check 4: Line item totals vs invoice total
    if invoice.line_items:
        line_total = sum(item.total for item in invoice.line_items)
        if abs(line_total - invoice.subtotal) > 1.0:
            issues.append(
                f"Line items sum to ${line_total:.2f}, "
                f"subtotal is ${invoice.subtotal:.2f}"
            )

    # Check 5: Duplicate detection
    table = dynamodb.Table(TABLE_NAME)
    existing = table.query(
        IndexName="vendor-invoice-index",
        KeyConditionExpression=(
            Key("vendor_name").eq(invoice.vendor_name)
            & Key("invoice_number").eq(invoice.invoice_number)
        ),
    )
    if existing["Count"] > 0:
        issues.append(
            f"Possible duplicate: {invoice.vendor_name} / "
            f"{invoice.invoice_number} already exists"
        )

    # Decision: approve or flag
    status = "approved" if not issues else "flagged_for_review"

    # Store in DynamoDB regardless (flagged items get reviewed)
    item = {
        "invoice_id": f"{invoice.vendor_name}#{invoice.invoice_number}",
        "vendor_name": invoice.vendor_name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat(),
        "total_amount": Decimal(str(invoice.total_amount)),
        "currency": invoice.currency,
        "status": status,
        "confidence": Decimal(str(invoice.confidence)),
        "extraction_method": invoice.extraction_method,
        "s3_key": s3_key,
        "issues": issues if issues else None,
        "line_items": [
            {
                "description": item.description,
                "quantity": Decimal(str(item.quantity)),
                "unit_price": Decimal(str(item.unit_price)),
                "total": Decimal(str(item.total)),
            }
            for item in invoice.line_items
        ],
        "processed_at": date.today().isoformat(),
    }

    # Remove None values (DynamoDB doesn't like them)
    item = {k: v for k, v in item.items() if v is not None}

    table.put_item(Item=item)

    # Alert if flagged
    if issues:
        sns.publish(
            TopicArn=REVIEW_TOPIC_ARN,
            Subject=f"Invoice Review: {invoice.vendor_name}",
            Message=(
                f"Invoice {invoice.invoice_number} from "
                f"{invoice.vendor_name} needs review.\n\n"
                f"Amount: ${invoice.total_amount:,.2f}\n"
                f"Issues:\n"
                + "\n".join(f"  - {issue}" for issue in issues)
            ),
        )

    return {
        "status": status,
        "invoice_id": item["invoice_id"],
        "issues": issues,
    }

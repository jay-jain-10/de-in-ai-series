# Show Me the Code

*Part 2 of 3: AI + Data Engineering on AWS*

---

## Problem Statement

In Part 1, I laid out the architecture: S3 → EventBridge → Step Functions → Textract → Claude → DynamoDB. Nice boxes and arrows. But boxes and arrows don't process invoices. Code does.

This part is the implementation. Every function, every prompt, every validation check. You can read through it, understand the decisions, and run it yourself if you want to try it on your own invoices.

## What You'll Get From This Article

- Complete working Python code for the entire invoice processing pipeline
- The Textract extraction logic (and how to handle its quirks)
- The Claude prompt that took 11 iterations to get right
- Pydantic schemas that act as data contracts between every stage
- The Step Functions state machine definition
- Error handling that doesn't silently swallow problems

---

Fair warning: I'm going to show you the code we actually shipped, not a cleaned-up version. Some of it is ugly. Some of it has comments that say things like "TODO: figure out why Textract sometimes returns empty tables." That's what production code looks like.

## The Data Contract

Before writing any processing logic, I defined what a "processed invoice" looks like. This schema is the contract between every stage of the pipeline. If Textract extracts data, it has to fit this shape. If Claude classifies something, the output must conform.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import date
from enum import Enum


class DocumentType(str, Enum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    PURCHASE_ORDER = "purchase_order"
    UNKNOWN = "unknown"


class LineItem(BaseModel):
    description: str
    quantity: float = 1.0
    unit_price: float
    total: float

    @field_validator("total")
    @classmethod
    def total_should_match(cls, v, info):
        qty = info.data.get("quantity", 1)
        price = info.data.get("unit_price", 0)
        expected = qty * price
        # Allow 1 cent tolerance for rounding
        if abs(v - expected) > 0.01:
            raise ValueError(
                f"Line total {v} doesn't match qty * price = {expected}"
            )
        return v


class InvoiceData(BaseModel):
    document_type: DocumentType
    invoice_number: str
    vendor_name: str
    vendor_address: Optional[str] = None
    invoice_date: date
    due_date: Optional[date] = None
    subtotal: float
    tax_amount: float = 0.0
    total_amount: float
    currency: str = "USD"
    line_items: list[LineItem] = []
    purchase_order_ref: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    extraction_method: str  # "textract", "claude_vision", "hybrid"
    raw_text_preview: str = Field(max_length=500)

    @field_validator("total_amount")
    @classmethod
    def total_should_roughly_match_subtotal_plus_tax(cls, v, info):
        subtotal = info.data.get("subtotal", 0)
        tax = info.data.get("tax_amount", 0)
        expected = subtotal + tax
        # Allow $1 tolerance — some invoices have rounding or fees
        if abs(v - expected) > 1.0 and subtotal > 0:
            raise ValueError(
                f"Total {v} doesn't match subtotal + tax = {expected}"
            )
        return v
```

I cannot overstate how much pain this schema saved us. Early on, we were passing around loose dictionaries. Claude would return `"invoice_date": "January 15, 2024"` one time and `"invoice_date": "2024-01-15"` the next. The Pydantic model catches that immediately.

The `confidence` field is important. Every stage writes its confidence, and downstream logic uses it to decide: auto-approve, send to human review, or reject.

## Stage 1: Textract Extraction

When a PDF lands in S3, the Step Functions state machine kicks off. First stop: Textract.

```python
import boto3
import time
from typing import Optional


textract = boto3.client("textract", region_name="us-east-1")


def extract_text_from_pdf(bucket: str, key: str) -> dict:
    """
    Extract text and tables from a PDF using Textract.
    PDFs always go through the async API (StartDocumentAnalysis) —
    the sync AnalyzeDocument API only supports images and single-page
    PDFs passed as raw bytes, not S3 references.
    """
    return _extract_async(bucket, key)


def _extract_async(bucket: str, key: str) -> dict:
    """Async extraction for multi-page documents."""
    start = textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
        FeatureTypes=["TABLES", "FORMS"],
    )

    job_id = start["JobId"]
    status = "IN_PROGRESS"

    # Poll for completion — yes, polling. Textract doesn't support
    # completion callbacks to Step Functions natively.
    # In production, we use Step Functions Wait + Task Token pattern instead.
    while status == "IN_PROGRESS":
        time.sleep(2)
        result = textract.get_document_analysis(JobId=job_id)
        status = result["JobStatus"]

    if status != "SUCCEEDED":
        raise Exception(f"Textract failed with status: {status}")

    return result


def parse_textract_response(response: dict) -> dict:
    """
    Convert Textract's verbose response into something usable.
    Returns: {"full_text": str, "tables": list[list[list[str]]], "forms": dict}
    """
    blocks = response.get("Blocks", [])

    # Extract full text
    lines = [
        block["Text"]
        for block in blocks
        if block["BlockType"] == "LINE"
    ]
    full_text = "\n".join(lines)

    # Extract tables
    tables = _extract_tables(blocks)

    # Extract key-value pairs (forms)
    forms = _extract_forms(blocks)

    return {
        "full_text": full_text,
        "tables": tables,
        "forms": forms,
        "page_count": max(
            (b.get("Page", 1) for b in blocks), default=1
        ),
        "confidence": _avg_confidence(blocks),
    }


def _extract_tables(blocks: list) -> list:
    """Pull structured tables from Textract blocks."""
    table_blocks = [b for b in blocks if b["BlockType"] == "TABLE"]
    cell_blocks = {b["Id"]: b for b in blocks if b["BlockType"] == "CELL"}
    word_blocks = {b["Id"]: b for b in blocks if b["BlockType"] == "WORD"}

    tables = []
    for table in table_blocks:
        rows = {}
        for rel in table.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cell_id in rel["Ids"]:
                    cell = cell_blocks.get(cell_id)
                    if not cell:
                        continue
                    row_idx = cell.get("RowIndex", 0)
                    col_idx = cell.get("ColumnIndex", 0)
                    # Get cell text
                    cell_text = _get_text_from_block(cell, word_blocks)
                    if row_idx not in rows:
                        rows[row_idx] = {}
                    rows[row_idx][col_idx] = cell_text

        # Convert to list of lists
        if rows:
            max_row = max(rows.keys())
            max_col = max(
                max(cols.keys()) for cols in rows.values()
            )
            table_data = []
            for r in range(1, max_row + 1):
                row_data = []
                for c in range(1, max_col + 1):
                    row_data.append(rows.get(r, {}).get(c, ""))
                table_data.append(row_data)
            tables.append(table_data)

    return tables


def _get_text_from_block(block: dict, word_blocks: dict) -> str:
    """Get text content from a block's child words."""
    words = []
    for rel in block.get("Relationships", []):
        if rel["Type"] == "CHILD":
            for word_id in rel["Ids"]:
                word = word_blocks.get(word_id)
                if word and "Text" in word:
                    words.append(word["Text"])
    return " ".join(words)


def _extract_forms(blocks: list) -> dict:
    """Extract key-value pairs from form data."""
    key_blocks = {}
    value_blocks = {}
    word_blocks = {b["Id"]: b for b in blocks if b["BlockType"] == "WORD"}

    for block in blocks:
        if block["BlockType"] == "KEY_VALUE_SET":
            if "KEY" in block.get("EntityTypes", []):
                key_blocks[block["Id"]] = block
            elif "VALUE" in block.get("EntityTypes", []):
                value_blocks[block["Id"]] = block

    forms = {}
    for key_id, key_block in key_blocks.items():
        key_text = _get_text_from_block(key_block, word_blocks)
        # Find associated value
        for rel in key_block.get("Relationships", []):
            if rel["Type"] == "VALUE":
                for val_id in rel["Ids"]:
                    val_block = value_blocks.get(val_id)
                    if val_block:
                        val_text = _get_text_from_block(
                            val_block, word_blocks
                        )
                        forms[key_text.strip()] = val_text.strip()
    return forms


def _avg_confidence(blocks: list) -> float:
    """Average confidence across all text blocks."""
    confidences = [
        b["Confidence"] / 100.0
        for b in blocks
        if "Confidence" in b and b["BlockType"] in ("LINE", "WORD")
    ]
    return sum(confidences) / len(confidences) if confidences else 0.0
```

A few things worth calling out.

The `_extract_tables` function is ugly. Textract returns tables as a flat list of cell blocks with row/column indices, not as a nested array. You have to reconstruct the table yourself. Every Textract tutorial glosses over this. In production, it's where 40% of your extraction bugs will live.

The confidence scoring averages across all text blocks. This is a rough heuristic. A smarter approach would weight by block area — a low-confidence word in a header matters more than a low-confidence word in the footer. We started simple and it's been good enough.

## Stage 2: Claude Classification and Extraction

This is where it gets interesting. Textract gives us raw text and tables. Claude turns that into structured invoice data.

```python
import boto3
import json
from pydantic import ValidationError


bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")


CLASSIFICATION_PROMPT = """You are an accounts payable specialist. 
Given the following text extracted from a document, determine:
1. What type of document this is (invoice, credit_note, purchase_order, or unknown)
2. Your confidence level (0.0 to 1.0)

Respond in JSON only. No explanation.

Document text:
{text}

Respond as: {{"document_type": "...", "confidence": 0.0}}"""


EXTRACTION_PROMPT = """You are an accounts payable specialist extracting 
structured data from an invoice.

Here is the raw text extracted from the document:
---
{text}
---

Here are tables found in the document:
---
{tables}
---

Here are form key-value pairs found:
---
{forms}
---

Extract the following fields. If a field isn't present, use null.
For line_items, extract each line with description, quantity, unit_price, 
and total. If you can't determine quantity, assume 1.

IMPORTANT: 
- Dates must be in YYYY-MM-DD format
- All monetary amounts must be numbers (no $ signs or commas)
- Invoice numbers should include any prefix (e.g., "INV-4521" not "4521")
- If the total doesn't match subtotal + tax, use the total printed on 
  the invoice (it's more likely correct than your math)

Respond in JSON only:
{{
    "invoice_number": "...",
    "vendor_name": "...",
    "vendor_address": "...",
    "invoice_date": "YYYY-MM-DD",
    "due_date": "YYYY-MM-DD or null",
    "subtotal": 0.00,
    "tax_amount": 0.00,
    "total_amount": 0.00,
    "currency": "USD",
    "line_items": [
        {{"description": "...", "quantity": 1, "unit_price": 0.00, "total": 0.00}}
    ],
    "purchase_order_ref": "... or null",
    "confidence": 0.0
}}"""


def classify_document(text: str) -> dict:
    """
    Quick classification using Haiku — is this an invoice,
    credit note, PO, or something else?
    """
    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "user",
                    "content": CLASSIFICATION_PROMPT.format(
                        text=text[:2000]  # First 2K chars is enough
                    ),
                }
            ],
        }),
    )

    result = json.loads(response["body"].read())
    content = result["content"][0]["text"]

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Haiku occasionally wraps JSON in markdown code blocks
        cleaned = content.strip("`").replace("json\n", "")
        return json.loads(cleaned)


def extract_invoice_data(
    text: str, tables: list, forms: dict, textract_confidence: float
) -> InvoiceData:
    """
    Use Claude Sonnet to extract structured invoice data.
    Falls back gracefully if extraction fails.
    """
    # Format tables for the prompt
    tables_str = ""
    for i, table in enumerate(tables):
        tables_str += f"\nTable {i + 1}:\n"
        for row in table:
            tables_str += " | ".join(str(cell) for cell in row) + "\n"

    forms_str = "\n".join(f"{k}: {v}" for k, v in forms.items())

    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-5-sonnet-20241022-v2:0",
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT.format(
                        text=text,
                        tables=tables_str or "No tables found",
                        forms=forms_str or "No form fields found",
                    ),
                }
            ],
        }),
    )

    result = json.loads(response["body"].read())
    content = result["content"][0]["text"]

    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        cleaned = content.strip("`").replace("json\n", "")
        raw = json.loads(cleaned)

    # Merge confidence: average of Textract and Claude confidence
    claude_conf = raw.get("confidence", 0.5)
    merged_confidence = (textract_confidence * 0.4) + (claude_conf * 0.6)

    # Build the validated model
    invoice = InvoiceData(
        document_type=DocumentType.INVOICE,
        invoice_number=raw["invoice_number"],
        vendor_name=raw["vendor_name"],
        vendor_address=raw.get("vendor_address"),
        invoice_date=raw["invoice_date"],
        due_date=raw.get("due_date"),
        subtotal=float(raw.get("subtotal", 0)),
        tax_amount=float(raw.get("tax_amount", 0)),
        total_amount=float(raw["total_amount"]),
        currency=raw.get("currency", "USD"),
        line_items=[LineItem(**item) for item in raw.get("line_items", [])],
        purchase_order_ref=raw.get("purchase_order_ref"),
        confidence=round(merged_confidence, 3),
        extraction_method="hybrid",
        raw_text_preview=text[:500],
    )

    return invoice
```

That extraction prompt took 11 iterations. The biggest lesson: tell Claude what to do when data conflicts. Early versions would hallucinate line items when the table extraction was messy. Adding "If the total doesn't match subtotal + tax, use the total printed on the invoice" fixed 90% of those cases. AI prompts are like writing requirements for a very literal contractor — you have to handle the edge cases explicitly.

The confidence merging is deliberate. We weight Claude's confidence higher (60%) because Textract's confidence measures OCR quality, not semantic correctness. A perfectly clear scan of a misleading invoice gets high Textract confidence but should get low Claude confidence.

## Stage 3: Validation

This runs as a Lambda function. It's the last gate before data hits DynamoDB.

```python
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
```

The duplicate detection (Check 5) caught 23 duplicate invoices in our first month. Vendors sometimes send the same invoice twice — once by email, once through the portal. Without that check, we'd have paid them double. That single check paid for the entire pipeline.

## The Step Functions State Machine

This ties everything together. The state machine definition:

```json
{
  "Comment": "Invoice processing pipeline",
  "StartAt": "ExtractText",
  "States": {
    "ExtractText": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:extract-text",
      "Parameters": {
        "bucket.$": "$.detail.bucket.name",
        "key.$": "$.detail.object.key"
      },
      "Retry": [
        {
          "ErrorEquals": ["TooManyRequestsException", "ThrottlingException"],
          "IntervalSeconds": 5,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "Next": "HandleExtractionFailure"
        }
      ],
      "Next": "ClassifyDocument"
    },
    "ClassifyDocument": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:classify-doc",
      "Retry": [
        {
          "ErrorEquals": ["ThrottlingException"],
          "IntervalSeconds": 3,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Next": "IsInvoice"
    },
    "IsInvoice": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.document_type",
          "StringEquals": "invoice",
          "Next": "ExtractInvoiceData"
        }
      ],
      "Default": "SkipNonInvoice"
    },
    "ExtractInvoiceData": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:extract-invoice",
      "Retry": [
        {
          "ErrorEquals": ["ThrottlingException", "ModelTimeoutException"],
          "IntervalSeconds": 5,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "Next": "HandleExtractionFailure"
        }
      ],
      "Next": "ValidateAndStore"
    },
    "ValidateAndStore": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:validate-store",
      "Next": "Done"
    },
    "SkipNonInvoice": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:log-skipped",
      "Comment": "Log non-invoice documents for audit trail",
      "Next": "Done"
    },
    "HandleExtractionFailure": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:us-east-1:123456789:function:handle-failure",
      "Comment": "Move to dead letter queue, alert team",
      "Next": "Done"
    },
    "Done": {
      "Type": "Succeed"
    }
  }
}
```

The `IsInvoice` choice state is important. About 15% of documents that hit our pipeline aren't invoices — they're delivery receipts, packing slips, or emails that got misrouted. The classification step filters these out before we waste Sonnet tokens on extraction.

The retry configuration on the Bedrock calls isn't random. `ThrottlingException` is the most common Bedrock error at our scale — it happens when you submit too many requests in a burst (like 8 AM Monday when 200 invoices arrive at once). Three retries with exponential backoff handles it every time.

## The Lambda Handlers

Each Step Functions step calls a Lambda. In practice, each handler lives in its own Lambda package with its own imports. I'm showing them together here so you can see the flow. Each one imports the functions from the modules above:

```python
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
```

Notice how each handler passes `**event` through to the next step. Step Functions passes the output of each state as the input to the next. By spreading the previous event, we accumulate data as it flows through the pipeline. By the time we reach validation, the event contains everything: the original S3 key, Textract output, classification result, and extracted invoice data.

## What's Coming in Part 3

The code above works. I know because it's been processing ~4,000 invoices a month for the last 8 months. But "works" and "works in production" are different things.

In Part 3, I'll cover what happened when we deployed: the cost optimization that took us from $12K/year to $1.8K/year, the monitoring that caught a silent failure at 2 AM, the tradeoff that cost us two weeks of rework, and the business outcomes that made the CFO send a thank-you email (the only time that's ever happened).

---

*This is Part 2 of 3. Part 1: "We Were Drowning in Invoices" covers the problem and system design. Part 3: "Making It Survive Monday Morning" covers production hardening, cost optimization, and business outcomes.*

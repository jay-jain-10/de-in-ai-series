"""Stage 2: Claude classification + structured extraction on Bedrock (Part 2)."""
from .schemas import InvoiceData, LineItem, DocumentType
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

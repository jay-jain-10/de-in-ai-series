"""Stage 1: Textract extraction + response parsing (Part 2)."""
import boto3
import time
from typing import Optional


textract = boto3.client("textract", region_name="us-east-1")


def extract_text_from_pdf(bucket: str, key: str) -> dict:
    """
    Extract text and tables from a PDF using Textract.
    Always uses the async API (StartDocumentAnalysis) so single- and multi-page
    PDFs share one code path. Note: synchronous AnalyzeDocument also accepts
    single-page PDFs (<=10 MB) from S3 and returns faster; use it if your
    invoices are single-page and latency matters.
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

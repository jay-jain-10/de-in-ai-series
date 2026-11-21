"""Data contracts between every pipeline stage (Part 2)."""
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

from datetime import date as Date
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import BaseModel

Confidence = Literal["high", "medium", "low"]
Severity = Literal["must_reject", "needs_scrutiny", "info"]
ChecklistKey = Literal["vendor_history", "price_check", "stock_check", "arithmetic", "payment_terms"]
Status = Literal["PAID", "REJECTED", "NEEDS_HUMAN_REVIEW", "DUPLICATE", "ERROR"]
DecidedBy = Literal["agents", "guardrail", "human", "system"]


class ExtractedField(BaseModel):
    value: str | None
    raw: str | None
    confidence: Confidence


class ExtractedLineItem(BaseModel):
    name: ExtractedField
    qty: ExtractedField
    unit_price: ExtractedField
    line_total: ExtractedField | None = None
    note: ExtractedField | None = None


class ExtractedInvoice(BaseModel):
    """The LLM's structured-output schema."""

    invoice_number: ExtractedField | None = None
    vendor: ExtractedField | None = None
    date: ExtractedField | None = None
    due_date: ExtractedField | None = None
    currency: ExtractedField | None = None
    subtotal: ExtractedField | None = None
    tax: ExtractedField | None = None
    shipping: ExtractedField | None = None
    total: ExtractedField | None = None
    payment_terms: ExtractedField | None = None
    notes: ExtractedField | None = None
    items: list[ExtractedLineItem] = []


class LineItem(BaseModel):
    name_raw: str
    sku: str | None = None
    sku_suggestion: str | None = None
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    note: str | None = None


class Invoice(BaseModel):
    invoice_number: str | None = None
    vendor: str | None = None
    date: Date | None = None
    due_date: Date | None = None
    currency: str = "USD"
    items: list[LineItem] = []
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None


class Uncertainty(BaseModel):
    field_path: str  # e.g. "items[1].unit_price"
    raw: str | None = None
    normalized: str | None = None
    confidence: Confidence
    reason: str


class Flag(BaseModel):
    severity: Severity
    code: str
    message: str
    field_path: str | None = None


class Recommendation(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reasoning: str
    evidence: list[str] = []
    checklist: dict[ChecklistKey, str] | None = None
    open_questions: list[str] = []


class VPReview(BaseModel):
    verdict: Literal["ACCEPT", "SEND_BACK"]
    final_decision: Literal["APPROVE", "REJECT"] | None = None
    reasoning: str
    feedback: str | None = None
    open_questions: list[str] = []


class Round(BaseModel):
    number: int
    recommendation: Recommendation
    review: VPReview | None = None


class ApprovalOutcome(BaseModel):
    decision: Literal["APPROVE", "REJECT", "NEEDS_HUMAN_REVIEW"]
    reasoning: str
    rounds: list[Round] = []
    open_questions: list[str] = []


class FinalOutcome(BaseModel):
    status: Status
    decided_by: DecidedBy
    reasoning: str
    payment_result: dict | None = None
    human_note: str | None = None


class PipelineState(TypedDict, total=False):
    run_id: str
    source_path: str
    format: str
    content_hash: str
    raw_text: str
    invoice: Invoice
    fingerprint: str
    duplicate_of: str | None
    extraction_issues: list[str]
    uncertainties: list[Uncertainty]
    flags: list[Flag]
    approval: ApprovalOutcome
    final: FinalOutcome
    error: str | None

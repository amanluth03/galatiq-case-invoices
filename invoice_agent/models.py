"""Pydantic models and the LangGraph state shared by all pipeline stages.
Extracted* is what the LLM returns; Invoice/LineItem are the typed, normalized result; the rest carry flags, approval rounds and the final outcome.
"""
from datetime import date as Date
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic import BaseModel, model_validator

# Closed vocabularies: pydantic rejects any value outside these, so bad LLM output fails loudly.
Confidence = Literal["high", "medium", "low"]
Severity = Literal["must_reject", "needs_scrutiny", "info"]
ChecklistKey = Literal["vendor_history", "price_check", "stock_check", "arithmetic", "payment_terms"]
Status = Literal["PAID", "REJECTED", "NEEDS_HUMAN_REVIEW", "DUPLICATE", "ERROR"]
DecidedBy = Literal["agents", "guardrail", "human", "system"]


class ExtractedField(BaseModel):
    """One LLM-read value: normalized text, the exact source snippet, and how sure the LLM was."""
    # raw is kept verbatim for the review screen and audit trail; value may be None when the field is absent.
    value: str | None
    raw: str | None
    confidence: Confidence


class ExtractedLineItem(BaseModel):
    """One invoice line as the LLM read it (all strings, not yet typed)."""
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
    # Everything optional: a missing field must stay None, never be invented.
    items: list[ExtractedLineItem] = []


class LineItem(BaseModel):
    """A typed invoice line after normalization."""
    name_raw: str
    # sku = exact inventory match; sku_suggestion = fuzzy guess only, never applied silently.
    sku: str | None = None
    sku_suggestion: str | None = None
    # Decimal, not float: money/quantities must be exact (e.g. 0.1 + 0.2 style errors would break reconciliation).
    qty: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    note: str | None = None


class Invoice(BaseModel):
    """The typed, normalized invoice that validation and approval work from."""
    invoice_number: str | None = None
    vendor: str | None = None
    date: Date | None = None
    due_date: Date | None = None
    # Defaults to USD; to_invoice records a low-confidence Uncertainty when it had to default.
    currency: str = "USD"
    items: list[LineItem] = []
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None


class Uncertainty(BaseModel):
    """A spot where the agent was unsure or changed the source text; shown highlighted on the review screen."""
    field_path: str  # e.g. "items[1].unit_price"
    raw: str | None = None
    normalized: str | None = None
    confidence: Confidence
    reason: str


class Flag(BaseModel):
    """A validation finding with severity (must_reject / needs_scrutiny / info)."""
    severity: Severity
    code: str
    message: str
    field_path: str | None = None


class Recommendation(BaseModel):
    """The analyst agent's approve/reject proposal with evidence."""
    decision: Literal["APPROVE", "REJECT"]
    reasoning: str
    evidence: list[str] = []
    checklist: dict[ChecklistKey, str] | None = None
    open_questions: list[str] = []


class VPReview(BaseModel):
    """The VP agent's critique: accept, or send back for another round."""
    verdict: Literal["ACCEPT", "SEND_BACK"]
    final_decision: Literal["APPROVE", "REJECT"] | None = None
    reasoning: str
    feedback: str | None = None
    open_questions: list[str] = []


class Round(BaseModel):
    """One analyst-VP exchange in the approval loop."""
    number: int
    recommendation: Recommendation
    review: VPReview | None = None


class ApprovalOutcome(BaseModel):
    """Result of the approval loop; NEEDS_HUMAN_REVIEW when the agents cannot settle."""
    decision: Literal["APPROVE", "REJECT", "NEEDS_HUMAN_REVIEW"]
    reasoning: str
    rounds: list[Round] = []
    open_questions: list[str] = []


class FinalOutcome(BaseModel):
    """The terminal result of a run (paid, rejected, duplicate, ...) and who decided it."""
    status: Status
    decided_by: DecidedBy
    reasoning: str
    payment_result: dict | None = None
    human_note: str | None = None


class HumanDecision(BaseModel):
    """What a human reviewer sends to resume a paused run; justification is required to approve over must_reject flags."""
    decision: Literal["APPROVE", "REJECT"]
    note: str = ""
    justification: str | None = None


class JustifiedHumanDecision(HumanDecision):
    """The resume schema when the invoice has must_reject flags: approving over them needs a written justification."""

    @model_validator(mode="after")
    def _require_justification(self):
        """Reject an APPROVE whose justification is missing or only whitespace."""
        if self.decision == "APPROVE" and not (self.justification or "").strip():
            raise ValueError("justification required")
        return self


class PipelineState(TypedDict, total=False):
    """Shared LangGraph state; total=False because each node fills in only its own keys."""
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
    # Set only when a human resumed the run from human_review.
    human_decision: HumanDecision
    final: FinalOutcome
    error: str | None

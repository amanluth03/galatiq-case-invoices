"""Extraction stage: asks an LLM extractor for structured fields, then self-corrects using code checks.
Sits after read and before validate; the real Grok extractor is plugged in as an Extractor callable (Task 9).
"""
from typing import Callable

from pydantic import BaseModel

from invoice_agent.utils.arithmetic import reconcile
from invoice_agent.agents.extractor.prompt import EXTRACTION_PROMPT  # noqa: F401  (re-exported for callers and tests)
from invoice_agent.infra.config import MAX_EXTRACTION_RETRIES
from invoice_agent.utils.normalize import to_invoice
from invoice_agent.models import ExtractedInvoice, Invoice, Uncertainty

# (raw_text, errors_from_previous_attempt) -> structured extraction.
Extractor = Callable[[str, list[str]], ExtractedInvoice]


class ExtractionFailed(RuntimeError):
    """Raised when the extractor itself fails on every attempt (no usable output at all)."""


class ExtractionResult(BaseModel):
    """Outcome of extraction: typed invoice, where the agent was unsure, leftover issues, and attempts used."""
    invoice: Invoice
    uncertainties: list[Uncertainty]
    issues: list[str]
    attempts: int


def run_extraction(
    raw_text: str,
    extractor: Extractor,
    skus: list[str],
    max_retries: int = MAX_EXTRACTION_RETRIES,
) -> ExtractionResult:
    """Run the extractor, feeding schema/arithmetic errors back for up to max_retries corrections."""
    errors: list[str] = []
    last: tuple[Invoice, list[Uncertainty], list[str]] | None = None
    attempts = 0
    # Total attempts = first try + max_retries corrections.
    while attempts < 1 + max_retries:
        attempts += 1
        try:
            ext = extractor(raw_text, errors)
        except Exception as exc:
            # A crashed attempt still uses up an attempt; tell the next one why.
            errors = [f"previous attempt failed: {exc}"]
            continue
        try:
            invoice, uncertainties = to_invoice(ext, skus)
        except Exception as exc:
            errors = [str(exc)]
            continue
        errors = reconcile(invoice)
        last = (invoice, uncertainties, errors)
        if not errors:
            break
    if last is None:
        raise ExtractionFailed(f"extractor produced no usable result after {attempts} attempts: {errors}")
    invoice, uncertainties, issues = last
    return ExtractionResult(invoice=invoice, uncertainties=uncertainties, issues=issues, attempts=attempts)

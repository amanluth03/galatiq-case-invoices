"""Ingestion agent stage: asks an LLM for structured fields, then self-corrects using code checks.
Sits after read and before validate; make_ingestion_agent builds the real Grok IngestionAgent, tests plug in fakes.
"""
from typing import Callable

from pydantic import BaseModel

from invoice_agent.utils.arithmetic import reconcile
from invoice_agent.agents.ingestion.prompt import INGESTION_PROMPT
from invoice_agent.infra.config import MAX_EXTRACTION_RETRIES
from invoice_agent.utils.normalize import to_invoice
from invoice_agent.models import ExtractedInvoice, Invoice, Uncertainty

# (raw_text, errors_from_previous_attempt) -> structured extraction.
IngestionAgent = Callable[[str, list[str]], ExtractedInvoice]

# Every invoice has at least one line; an extraction without any means the agent read nothing (or returned empty).
NO_ITEMS_ERROR = "no line items were extracted: read every item line (name, quantity, unit price) from the text"


class ExtractionFailed(RuntimeError):
    """Raised when the ingestion agent itself fails on every attempt (no usable output at all)."""


class ExtractionResult(BaseModel):
    """Outcome of extraction: typed invoice, where the agent was unsure, leftover issues, and attempts used."""
    invoice: Invoice
    uncertainties: list[Uncertainty]
    issues: list[str]
    attempts: int


def run_extraction(
    raw_text: str,
    ingestion_agent: IngestionAgent,
    skus: list[str],
    max_retries: int = MAX_EXTRACTION_RETRIES,
) -> ExtractionResult:
    """Run the ingestion agent, feeding schema/arithmetic errors back for up to max_retries corrections."""
    errors: list[str] = []
    last: tuple[Invoice, list[Uncertainty], list[str]] | None = None
    attempts = 0
    # Total attempts = first try + max_retries corrections.
    while attempts < 1 + max_retries:
        attempts += 1
        try:
            ext = ingestion_agent(raw_text, errors)
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
        # An empty answer passes arithmetic trivially (nothing to add up), so it needs its own check.
        if not invoice.items:
            errors = [NO_ITEMS_ERROR, *errors]
        last = (invoice, uncertainties, errors)
        if not errors:
            break
    if last is None:
        raise ExtractionFailed(f"ingestion agent produced no usable result after {attempts} attempts: {errors}")
    invoice, uncertainties, issues = last
    return ExtractionResult(invoice=invoice, uncertainties=uncertainties, issues=issues, attempts=attempts)


def make_ingestion_agent(model) -> IngestionAgent:
    """Build the Grok IngestionAgent: structured output into ExtractedInvoice, with the previous attempt's errors fed back."""
    # json_schema, not the default function_calling: in live runs Grok's function-calling answer sometimes came
    # back as an empty tool call ({}), which validates as a blank invoice because every field is optional.
    structured = model.with_structured_output(ExtractedInvoice, method="json_schema")

    def extract(raw_text: str, errors: list[str]) -> ExtractedInvoice:
        """Send the prompt and raw text (plus a correction request on retries) and return the parsed extraction."""
        messages = [("system", INGESTION_PROMPT), ("user", raw_text)]
        if errors:
            # Self-correction: tell the model exactly which checks its last answer failed.
            listed = "\n".join(f"- {e}" for e in errors)
            messages.append(("user", f"Your previous attempt had these problems. Re-read the text and fix them:\n{listed}"))
        return structured.invoke(messages)

    return extract

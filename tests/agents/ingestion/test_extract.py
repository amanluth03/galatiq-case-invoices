"""Tests for the self-correcting extraction loop (retries, give-up, ingestion agent failures) and the prompt contract.
"""
import pytest

from invoice_agent.agents.ingestion.agent import INGESTION_PROMPT, ExtractionFailed, run_extraction
from invoice_agent.models import ExtractedInvoice
from tests.fakes import ScriptedIngestionAgent, make_extracted

SKUS = ["WidgetA", "WidgetB", "GadgetX"]


def test_clean_extraction_single_attempt():
    """A clean extraction succeeds on the first attempt with no issues."""
    ex = ScriptedIngestionAgent([make_extracted()])
    res = run_extraction("raw text", ex, SKUS)
    assert res.attempts == 1 and res.issues == []
    assert ex.calls[0].raw_text == "raw text" and ex.calls[0].errors == []
    assert res.invoice.invoice_number == "INV-1001"


def test_wrong_printed_math_is_not_sent_back():
    """Schema-valid amounts that don't add up are kept as printed: one call, mismatch returned as an issue."""
    ex = ScriptedIngestionAgent([make_extracted(total="5500.00")])
    res = run_extraction("raw", ex, SKUS)
    assert len(ex.calls) == 1 and res.attempts == 1
    assert res.issues and "total" in res.issues[0]
    # The vendor's printed total survives untouched for validation to flag.
    assert str(res.invoice.total) == "5500.00"


def test_empty_extraction_is_retried():
    """An extraction with no line items (Grok sometimes returns an empty tool call) is sent back, not accepted."""
    ex = ScriptedIngestionAgent([ExtractedInvoice(), make_extracted()])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 2
    assert any("no line items" in e for e in ex.calls[1].errors)
    assert res.issues == [] and res.invoice.vendor == "Widgets Inc."


def test_persistent_empty_extraction_is_reported_as_issue():
    """If every attempt comes back without line items, the result says so as an issue (validation then rejects it)."""
    ex = ScriptedIngestionAgent([ExtractedInvoice()] * 3)
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 3
    assert any("no line items" in i for i in res.issues)


def test_ingestion_agent_exception_then_success():
    """An ingestion agent exception counts as an attempt and is fed back to the next call."""
    ex = ScriptedIngestionAgent([RuntimeError("timeout"), make_extracted()])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 2
    assert ex.calls[1].errors == ["previous attempt failed: timeout"]


def test_all_attempts_raise():
    """If the ingestion agent raises on every attempt, ExtractionFailed is raised."""
    ex = ScriptedIngestionAgent([RuntimeError("boom")] * 3)
    with pytest.raises(ExtractionFailed) as err:
        run_extraction("raw", ex, SKUS)
    # A plain sentence for the reviewer, not Python list text like "['previous attempt failed: boom']".
    assert str(err.value) == ("The ingestion agent couldn't read this invoice after 3 attempts. "
                              "Last problem: previous attempt failed: boom.")


def test_later_exception_keeps_last_successful_result():
    """If a retry raises after an earlier attempt produced an invoice, that earlier result is kept with its issues."""
    ex = ScriptedIngestionAgent([ExtractedInvoice(), RuntimeError("down"), RuntimeError("down")])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 3 and any("no line items" in i for i in res.issues)


def test_prompt_states_confidence_contract():
    """The prompt tells the LLM the confidence rules and never to invent values."""
    for needle in ("medium", "low", "never invent", "null", "note"):
        assert needle in INGESTION_PROMPT.lower()


def test_prompt_says_copy_amounts_as_printed():
    """The prompt forbids changing printed digits to make the arithmetic work."""
    assert "exactly as printed" in INGESTION_PROMPT and "never to changing digits" in INGESTION_PROMPT

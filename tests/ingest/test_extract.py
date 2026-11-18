"""Tests for the self-correcting extraction loop (retries, give-up, extractor failures) and the prompt contract.
"""
import pytest

from invoice_agent.ingest.extract import EXTRACTION_PROMPT, ExtractionFailed, run_extraction
from tests.fakes import ScriptedExtractor, make_extracted

SKUS = ["WidgetA", "WidgetB", "GadgetX"]


def test_clean_extraction_single_attempt():
    """A clean extraction succeeds on the first attempt with no issues."""
    ex = ScriptedExtractor([make_extracted()])
    res = run_extraction("raw text", ex, SKUS)
    assert res.attempts == 1 and res.issues == []
    assert ex.calls[0].raw_text == "raw text" and ex.calls[0].errors == []
    assert res.invoice.invoice_number == "INV-1001"


def test_retries_with_arithmetic_errors():
    """A wrong total triggers a retry that receives the arithmetic message, and the fixed result has no issues."""
    ex = ScriptedExtractor([make_extracted(total="5500.00"), make_extracted()])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 2
    assert any("total" in e for e in ex.calls[1].errors)
    assert res.issues == []


def test_gives_up_after_max_retries_and_reports_issue():
    """Persistent mismatches stop after 1 + max_retries attempts and are returned as issues."""
    ex = ScriptedExtractor([make_extracted(total="5500.00")] * 3)
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 3
    assert res.issues and "total" in res.issues[0]
    assert res.invoice.total is not None


def test_extractor_exception_then_success():
    """An extractor exception counts as an attempt and is fed back to the next call."""
    ex = ScriptedExtractor([RuntimeError("timeout"), make_extracted()])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 2
    assert ex.calls[1].errors == ["previous attempt failed: timeout"]


def test_all_attempts_raise():
    """If the extractor raises on every attempt, ExtractionFailed is raised."""
    ex = ScriptedExtractor([RuntimeError("boom")] * 3)
    with pytest.raises(ExtractionFailed):
        run_extraction("raw", ex, SKUS)


def test_later_exception_keeps_last_successful_result():
    """If a retry raises after an earlier attempt produced an invoice, that earlier result is kept with its issues."""
    ex = ScriptedExtractor([make_extracted(total="5500.00"), RuntimeError("down"), RuntimeError("down")])
    res = run_extraction("raw", ex, SKUS)
    assert res.attempts == 3 and res.issues


def test_prompt_states_confidence_contract():
    """The prompt tells the LLM the confidence rules and never to invent values."""
    for needle in ("medium", "low", "never invent", "null", "note"):
        assert needle in EXTRACTION_PROMPT.lower()

"""Tests for the Decimal money model, strict VPReview verdicts, and config defaults.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_agent.infra.config import Settings, load_settings
from invoice_agent.models import Invoice, VPReview


def test_invoice_money_is_decimal():
    """Money strings become exact Decimals, not floats."""
    inv = Invoice(invoice_number="INV-1", vendor="V", date=None, due_date=None, currency="USD",
                  items=[], total="22562.80")
    assert inv.total == Decimal("22562.80") and isinstance(inv.total, Decimal)


def test_vp_review_rejects_unknown_verdict():
    """A verdict outside ACCEPT/SEND_BACK fails validation."""
    with pytest.raises(ValidationError):
        VPReview(verdict="MAYBE", final_decision=None, reasoning="", feedback=None, open_questions=[])


def test_load_settings_defaults(monkeypatch):
    """With no env var and no .env, the model defaults to grok-4-1-fast-non-reasoning (also on an empty XAI_MODEL)."""
    # env_file=None so the test never reads the developer's real .env.
    monkeypatch.delenv("XAI_MODEL", raising=False)
    assert load_settings(env_file=None).xai_model == "grok-4-1-fast-non-reasoning"
    assert Settings().xai_model == "grok-4-1-fast-non-reasoning"
    monkeypatch.setenv("XAI_MODEL", "")
    assert load_settings(env_file=None).xai_model == "grok-4-1-fast-non-reasoning"


def test_batch_workers_default_and_env(monkeypatch):
    """Batch runs use 5 workers by default; BATCH_WORKERS overrides it (1 = the old one-at-a-time behavior)."""
    monkeypatch.delenv("BATCH_WORKERS", raising=False)
    assert load_settings(env_file=None).batch_workers == 5 and Settings().batch_workers == 5
    monkeypatch.setenv("BATCH_WORKERS", "1")
    assert load_settings(env_file=None).batch_workers == 1

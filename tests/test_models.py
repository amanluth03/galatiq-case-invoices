"""Tests for the Decimal money model, strict VPReview verdicts, and config defaults.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_agent.config import load_settings
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
    """With no env var and no .env, the model defaults to grok-3."""
    # env_file=None so the test never reads the developer's real .env.
    monkeypatch.delenv("XAI_MODEL", raising=False)
    assert load_settings(env_file=None).xai_model == "grok-3"

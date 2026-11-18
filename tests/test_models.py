from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_agent.config import load_settings
from invoice_agent.models import Invoice, VPReview


def test_invoice_money_is_decimal():
    inv = Invoice(invoice_number="INV-1", vendor="V", date=None, due_date=None, currency="USD",
                  items=[], total="22562.80")
    assert inv.total == Decimal("22562.80") and isinstance(inv.total, Decimal)


def test_vp_review_rejects_unknown_verdict():
    with pytest.raises(ValidationError):
        VPReview(verdict="MAYBE", final_decision=None, reasoning="", feedback=None, open_questions=[])


def test_load_settings_defaults(monkeypatch):
    monkeypatch.delenv("XAI_MODEL", raising=False)
    assert load_settings(env_file=None).xai_model == "grok-3"

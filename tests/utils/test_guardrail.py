"""Tests for the guardrail rules: must_reject blocks an agent APPROVE; price/total/currency deviations need a human to confirm."""
from invoice_agent.models import Flag
from invoice_agent.utils.guardrail import HUMAN_CONFIRMATION_REASON, apply_guardrail, needs_human_confirmation


def flag(severity, message, code="c"):
    """A flag of the given severity (fixed code unless given)."""
    return Flag(severity=severity, code=code, message=message)


def test_no_must_reject_lets_payment_through():
    """Scrutiny and info flags alone do not block: the guardrail returns None."""
    assert apply_guardrail([flag("needs_scrutiny", "price"), flag("info", "urgent")]) is None
    assert apply_guardrail([]) is None


def test_must_reject_forces_rejection_with_messages():
    """Must-reject flags produce a REJECTED outcome decided by the guardrail, listing every blocking message."""
    out = apply_guardrail([flag("must_reject", "A short"), flag("needs_scrutiny", "x"), flag("must_reject", "B gone")])
    assert out.status == "REJECTED" and out.decided_by == "guardrail"
    assert out.reasoning == "Blocked by guardrail: A short; B gone"


def test_price_or_arithmetic_mismatch_needs_human_confirmation():
    """A price, arithmetic, unprinted-amount or non-USD flag means an agent APPROVE must be confirmed by a person."""
    for code in ("price_mismatch", "arithmetic_mismatch", "amount_not_in_source", "non_usd_currency"):
        assert needs_human_confirmation([flag("needs_scrutiny", "x", code)]) == HUMAN_CONFIRMATION_REASON
    assert HUMAN_CONFIRMATION_REASON == (
        "Agents approved, but the invoice's prices, totals or currency differ from Acme's records: a person must confirm"
    )


def test_other_flags_do_not_need_human_confirmation():
    """over_10k, other scrutiny flags and info flags let an agent APPROVE through to payment."""
    flags = [flag("needs_scrutiny", "big", "over_10k"), flag("needs_scrutiny", "d", "due_date_missing"),
             flag("info", "urgent", "urgency_language")]
    assert needs_human_confirmation(flags) is None
    assert needs_human_confirmation([]) is None

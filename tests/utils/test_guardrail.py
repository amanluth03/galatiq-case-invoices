"""Tests for the guardrail rule: any must_reject flag blocks an agent APPROVE before payment."""
from invoice_agent.models import Flag
from invoice_agent.utils.guardrail import apply_guardrail


def flag(severity, message):
    """A flag of the given severity with a fixed code."""
    return Flag(severity=severity, code="c", message=message)


def test_no_must_reject_lets_payment_through():
    """Scrutiny and info flags alone do not block: the guardrail returns None."""
    assert apply_guardrail([flag("needs_scrutiny", "price"), flag("info", "urgent")]) is None
    assert apply_guardrail([]) is None


def test_must_reject_forces_rejection_with_messages():
    """Must-reject flags produce a REJECTED outcome decided by the guardrail, listing every blocking message."""
    out = apply_guardrail([flag("must_reject", "A short"), flag("needs_scrutiny", "x"), flag("must_reject", "B gone")])
    assert out.status == "REJECTED" and out.decided_by == "guardrail"
    assert out.reasoning == "Blocked by guardrail: A short; B gone"

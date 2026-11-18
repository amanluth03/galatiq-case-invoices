"""Tests for the guardrail rules: must_reject blocks an agent APPROVE; money-only differences go to a person whatever the agents decided."""
from invoice_agent.models import Flag
from invoice_agent.utils.guardrail import HUMAN_REVIEW_REASON, apply_guardrail, needs_person


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


def test_money_differences_need_a_person_for_either_recommendation():
    """A price, arithmetic, unprinted-amount or non-USD flag sends an agent APPROVE or REJECT to a person."""
    for code in ("price_mismatch", "arithmetic_mismatch", "amount_not_in_source", "non_usd_currency"):
        for rec in ("APPROVE", "REJECT"):
            reason = needs_person([flag("needs_scrutiny", "x", code)], rec)
            assert reason == HUMAN_REVIEW_REASON.format(recommendation=rec)
    assert needs_person([flag("needs_scrutiny", "x", "price_mismatch")], "REJECT") == (
        "Prices, totals or currency differ from Acme's records. The agents recommended REJECT; a person decides."
    )


def test_must_reject_never_goes_to_a_person_for_money_differences():
    """A must_reject flag alongside a price flag means no person step: the invoice is rejected."""
    flags = [flag("needs_scrutiny", "p", "price_mismatch"), flag("must_reject", "stock", "exceeds_stock")]
    assert needs_person(flags, "REJECT") is None


def test_other_flags_do_not_need_a_person():
    """over_10k, other scrutiny flags and info flags let the agents' decision stand."""
    flags = [flag("needs_scrutiny", "big", "over_10k"), flag("needs_scrutiny", "d", "due_date_missing"),
             flag("info", "urgent", "urgency_language")]
    assert needs_person(flags, "APPROVE") is None
    assert needs_person([], "REJECT") is None

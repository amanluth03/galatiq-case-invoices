"""Guardrail stage: the last code check between an agent APPROVE and payment.
Any must_reject flag overrides the agents and forces REJECT; a price or total deviation sends the APPROVE
to a human instead of paying it automatically
(price, arithmetic, unprinted-amount or non-USD flags). A human APPROVE skips this stage."""
from invoice_agent.models import Flag, FinalOutcome

# Money deviations the agents may explain, but must not settle alone: a person confirms before paying.
# amount_not_in_source: the reader's amount isn't printed on the invoice; non_usd_currency: prices can't be compared.
_HUMAN_CONFIRMATION_CODES = {"price_mismatch", "arithmetic_mismatch", "amount_not_in_source", "non_usd_currency"}
HUMAN_CONFIRMATION_REASON = (
    "Agents approved, but the invoice's prices, totals or currency differ from Acme's records — a person must confirm"
)


def apply_guardrail(flags: list[Flag]) -> FinalOutcome | None:
    """Return a forced REJECTED outcome if any flag is must_reject, else None (payment may proceed)."""
    blocking = [f.message for f in flags if f.severity == "must_reject"]
    if not blocking:
        return None
    return FinalOutcome(
        status="REJECTED", decided_by="guardrail",
        reasoning="Blocked by guardrail: " + "; ".join(blocking),
    )


def needs_human_confirmation(flags: list[Flag]) -> str | None:
    """Return the reason a person must confirm an agent APPROVE (price, total or currency deviation), else None."""
    if any(f.code in _HUMAN_CONFIRMATION_CODES for f in flags):
        return HUMAN_CONFIRMATION_REASON
    return None

"""Guardrail stage: the last code check between an agent APPROVE and payment.
Any must_reject flag overrides the agents and forces REJECT (a human APPROVE skips this stage)."""
from invoice_agent.models import Flag, FinalOutcome


def apply_guardrail(flags: list[Flag]) -> FinalOutcome | None:
    """Return a forced REJECTED outcome if any flag is must_reject, else None (payment may proceed)."""
    blocking = [f.message for f in flags if f.severity == "must_reject"]
    if not blocking:
        return None
    return FinalOutcome(
        status="REJECTED", decided_by="guardrail",
        reasoning="Blocked by guardrail: " + "; ".join(blocking),
    )

"""Guardrail rules: the code checks that sit above the agents.
Any must_reject flag forces REJECT (validate applies it straight away, so those invoices skip the agents); an invoice whose only problems are money differences
(price, arithmetic, unprinted-amount or non-USD flags) goes to a person whatever the agents decided. A human APPROVE skips this stage."""
from invoice_agent.models import Flag, FinalOutcome

# Money deviations the agents may explain, but must not settle alone: a person confirms before paying.
# amount_not_in_source: the reader's amount isn't printed on the invoice; non_usd_currency: prices can't be compared.
_HUMAN_CONFIRMATION_CODES = {"price_mismatch", "arithmetic_mismatch", "amount_not_in_source", "non_usd_currency"}
HUMAN_REVIEW_REASON = ("Prices, totals or currency differ from Acme's records. "
                       "The agents recommended {recommendation}; a person decides.")


def apply_guardrail(flags: list[Flag]) -> FinalOutcome | None:
    """Return a forced REJECTED outcome listing every must_reject flag in plain words, else None (payment may proceed)."""
    blocking = [f.message for f in flags if f.severity == "must_reject"]
    if not blocking:
        return None
    return FinalOutcome(
        status="REJECTED", decided_by="guardrail",
        # e.g. "Rejected by the rules: WidgetC isn't in inventory. Ordered 20 GadgetX, only 5 in stock."
        reasoning="Rejected by the rules: " + ". ".join(m.rstrip(".") for m in blocking) + ".",
    )


def needs_person(flags: list[Flag], recommendation: str) -> str | None:
    """Return the reviewer's reason when only money differences are flagged (agent APPROVE or REJECT), else None."""
    # A must_reject flag always rejects, so it never goes to a person for this reason.
    if any(f.severity == "must_reject" for f in flags):
        return None
    if any(f.code in _HUMAN_CONFIRMATION_CODES for f in flags):
        return HUMAN_REVIEW_REASON.format(recommendation=recommendation)
    return None

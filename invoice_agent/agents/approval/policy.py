"""Approval rulebook text and checklist helper; sits between validation and the analyst/VP agents."""
from invoice_agent.models import ChecklistKey, Recommendation

# The five items the analyst must address for invoices over $10K.
CHECKLIST_KEYS: tuple[ChecklistKey, ...] = (
    "vendor_history", "price_check", "stock_check", "arithmetic", "payment_terms",
)

APPROVAL_POLICY = """\
INVOICE APPROVAL POLICY

Validation flags come in three severities. Reference prices in inventory are ground truth.

MUST_REJECT flags: always REJECT. No explanation makes these approvable.
- exceeds_stock: total quantity of an item (summed across duplicate lines) is more than stock on hand.
- unknown_item: an item is not in inventory.
- zero_stock_item: an item has zero stock, or looks suspicious (e.g. FakeItem).
- non_positive_quantity: a line has quantity zero or below.
- negative_total: the invoice total is negative.
- missing_vendor: the vendor is missing or blank.
- missing_total: the invoice total is missing.
- missing_quantity: a line has no quantity, so it cannot be checked against stock.
- no_line_items: the invoice has no line items.
- total_exceeds_line_items: the stated total is more than the line items + tax + shipping add up to (overbilling, e.g. 1013).
- line_total_exceeds_price: a line's stated total is more than its quantity x unit price.
- total_unverifiable: the amounts are too large to compute, so the total cannot be checked.
- invoice_number_reused: this invoice number was already processed with different contents (possible double-billing, e.g. 1004 vs 1004_revised).

NEEDS_SCRUTINY flags: not an automatic reject, but each one must be explicitly explained in the recommendation. An unexplained deviation means REJECT.
- price_mismatch: a unit price differs from the reference price. Any deviation counts, there is no tolerance. Discounts and rush fees are still deviations; unless the invoice itself justifies it, REJECT.
- arithmetic_mismatch: line items, subtotal, tax and total do not reconcile.
- over_10k: the total is over $10,000. The over-$10K checklist below is required.
- due_date_missing: the due date is missing or unparseable.
- due_before_or_on_invoice_date: the due date is on or before the invoice date (e.g. "yesterday").
- non_usd_currency: the invoice is not in USD, so amounts cannot be compared to USD reference prices.
- missing_unit_price: a line has no unit price, so it cannot be price-checked.
- extraction_issue: the reader was unsure about a field or had to guess; treat the value with caution.

INFO flags: consider them, they do not decide on their own.
- urgency_language: pressure wording such as "urgent" or wire-transfer demands.
- vendor_name_change: the vendor name differs from earlier invoices.

OVER-$10K CHECKLIST: the analyst must give a non-empty answer for each key.
- vendor_history: prior invoices from this vendor and any duplicate invoice numbers (use get_invoice_history).
- price_check: each line's unit price against the reference price.
- stock_check: stock is sufficient, including quantities aggregated across duplicate lines.
- arithmetic: line totals, subtotal, tax and total reconcile (use calculate).
- payment_terms: due date is sane, and any urgency or payment-pressure red flags are noted.
The VP cannot ACCEPT an over-$10K recommendation with any checklist item empty.

A well-argued REJECT is a good outcome. When evidence is missing or a deviation is unexplained, REJECT with clear reasoning and list open questions. Never approve just to be agreeable.
"""


def missing_checklist_keys(rec: Recommendation) -> list[str]:
    """Checklist keys that are absent or blank (all five when there is no checklist)."""
    checklist = rec.checklist or {}
    return [k for k in CHECKLIST_KEYS if not (checklist.get(k) or "").strip()]


def checklist_complete(rec: Recommendation) -> bool:
    """True when every checklist key is present with non-blank text."""
    return not missing_checklist_keys(rec)

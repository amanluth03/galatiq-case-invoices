"""Deterministic invoice arithmetic check: line totals, subtotal, and total all reconcile to the cent.
Runs in the validate stage; mismatches become needs_scrutiny flags, and lines_plus_charges feeds the overbilling gate.
"""
from decimal import Decimal, InvalidOperation

from invoice_agent.models import Invoice

# Decimal throughout: float rounding would create false mismatches.
_CENT = Decimal("0.01")


def _q(d: Decimal) -> Decimal:
    """Round to cents."""
    return d.quantize(_CENT)


def _line_amount(it) -> Decimal | None:
    """A line's amount: the stated line_total, else qty x unit_price, else None."""
    if it.line_total is not None:
        return _q(it.line_total)
    if it.qty is not None and it.unit_price is not None:
        return _q(it.qty * it.unit_price)
    return None


def lines_plus_charges(inv: Invoice) -> Decimal | None:
    """Sum of line amounts + tax + shipping (the same amounts reconcile uses); None when no line has an amount."""
    try:
        amounts = [a for a in (_line_amount(it) for it in inv.items) if a is not None]
        if not amounts:
            return None
        tax = _q(inv.tax) if inv.tax is not None else Decimal("0.00")
        ship = _q(inv.shipping) if inv.shipping is not None else Decimal("0.00")
        return sum(amounts, Decimal("0.00")) + tax + ship
    # Absurd magnitudes are already reported by reconcile; nothing to compare here.
    except InvalidOperation:
        return None


def reconcile(inv: Invoice) -> list[str]:
    """Return human-readable mismatch messages (empty list means the math checks out)."""
    msgs: list[str] = []
    try:
        _reconcile(inv, msgs)
    # Absurd magnitudes (e.g. 1E+30) overflow Decimal precision; report instead of crashing.
    except InvalidOperation:
        msgs.append("total: value could not be checked")
    return msgs


def _reconcile(inv: Invoice, msgs: list[str]) -> None:
    """Append a message for each line, subtotal, or total that does not add up."""
    amounts: list[Decimal] = []
    for i, it in enumerate(inv.items):
        computed = None
        if it.qty is not None and it.unit_price is not None:
            computed = _q(it.qty * it.unit_price)
            if it.line_total is not None and computed != _q(it.line_total):
                msgs.append(
                    f"items[{i}]: {it.qty} × {_q(it.unit_price)} = {computed}, "
                    f"but line_total is {_q(it.line_total)}"
                )
        # Prefer the stated line_total, else qty x unit_price.
        amount = _line_amount(it)
        if amount is not None:
            amounts.append(amount)
    lines_sum = sum(amounts, Decimal("0.00"))
    # Skip the subtotal check when there are no lines: nothing to sum against.
    if inv.subtotal is not None and amounts and lines_sum != _q(inv.subtotal):
        msgs.append(f"subtotal: expected {lines_sum} (sum of line amounts), found {_q(inv.subtotal)}")
    # Total = subtotal (or sum of lines) + tax + shipping; a lone total cannot be checked.
    # Sample 1013: stated total 22562.80 is really 22512.80, $50 too high.
    if inv.total is not None and (inv.subtotal is not None or amounts):
        base = _q(inv.subtotal) if inv.subtotal is not None else lines_sum
        tax = _q(inv.tax) if inv.tax is not None else Decimal("0.00")
        ship = _q(inv.shipping) if inv.shipping is not None else Decimal("0.00")
        expected = base + tax + ship
        if expected != _q(inv.total):
            label = "subtotal" if inv.subtotal is not None else "sum of lines"
            msgs.append(
                f"total: expected {expected} ({label} {base} + tax {tax} + shipping {ship}), "
                f"found {_q(inv.total)}"
            )

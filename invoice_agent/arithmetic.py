from decimal import Decimal

from invoice_agent.models import Invoice

_CENT = Decimal("0.01")


def _q(d: Decimal) -> Decimal:
    return d.quantize(_CENT)


def reconcile(inv: Invoice) -> list[str]:
    msgs: list[str] = []
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
        if it.line_total is not None:
            amounts.append(_q(it.line_total))
        elif computed is not None:
            amounts.append(computed)
    lines_sum = sum(amounts, Decimal("0.00"))
    if inv.subtotal is not None and amounts and lines_sum != _q(inv.subtotal):
        msgs.append(f"subtotal: expected {lines_sum} (sum of line amounts), found {_q(inv.subtotal)}")
    if inv.total is not None:
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
    return msgs

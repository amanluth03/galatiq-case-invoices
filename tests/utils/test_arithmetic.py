"""Tests for reconcile(): line, subtotal and total arithmetic, using real sample invoices as fixtures.
"""
from decimal import Decimal as D

from invoice_agent.utils.arithmetic import reconcile
from invoice_agent.models import Invoice, LineItem


def L(q, p, t=None):
    """Shorthand for building a LineItem from qty, price and optional line total."""
    return LineItem(name_raw="x", qty=D(q), unit_price=D(p), line_total=None if t is None else D(t))


def test_reconcile_1013_exact():
    """Invoice 1013 adds up to 22512.80; its stated 22562.80 is flagged as $50 too high."""
    items = [L(1, "2000.00"), L(2, "1500.00"), L(4, "1000.00"), L(1, "5000.00"),
             L(3, "1500.00"), L(2, "500.00"), L(1, "540.00"), L(1, "1000.00")]
    inv = Invoice(items=items, subtotal=D("21040.00"), tax=D("1472.80"), total=D("22512.80"))
    assert reconcile(inv) == []
    # the shipped invoice_1013.json states total 22562.80, which is 50.00 too high
    inv.total = D("22562.80")
    assert reconcile(inv) == [
        "total: expected 22512.80 (subtotal 21040.00 + tax 1472.80 + shipping 0.00), found 22562.80"]


def test_reconcile_1009_flags_mismatch():
    """Invoice 1009 (negative qty) produces a subtotal mismatch."""
    inv = Invoice(items=[L(-5, 250), L(2, 500)], subtotal=D("1000.00"), total=D("-250.00"))
    msgs = reconcile(inv)
    assert any(m.startswith("subtotal:") for m in msgs)


def test_reconcile_1010_includes_shipping():
    """Shipping counts toward the total (invoice 1010)."""
    inv = Invoice(subtotal=D("6700"), tax=D("335"), shipping=D("150"), total=D("7185"))
    assert reconcile(inv) == []


def test_reconcile_line_and_total_messages():
    """Wrong line_total and wrong total each produce their exact message."""
    inv = Invoice(items=[L(10, "250.00", "2000.00")], subtotal=D("2000.00"), total=D("2500.00"))
    msgs = reconcile(inv)
    assert "items[0]: 10 × 250.00 = 2500.00, but line_total is 2000.00" in msgs
    assert ("total: expected 2000.00 (subtotal 2000.00 + tax 0.00 + shipping 0.00), found 2500.00") in msgs


def test_reconcile_no_subtotal_uses_lines():
    """With no stated subtotal, the sum of lines is the base for the total check."""
    inv = Invoice(items=[L(2, "5.00")], total=D("10.00"))
    assert reconcile(inv) == []


def test_reconcile_total_only_is_not_checked():
    """A lone total with no lines or subtotal cannot be checked, so no message."""
    assert reconcile(Invoice(total=D("5000"))) == []


def test_reconcile_off_by_one_cent():
    """Even a one-cent difference is reported (no tolerance)."""
    inv = Invoice(subtotal=D("5000.00"), total=D("5000.01"))
    assert len(reconcile(inv)) == 1


def test_reconcile_does_not_raise_on_huge_values():
    """Absurdly large values yield a message instead of an exception."""
    inv = Invoice(subtotal=D("1E+30"), total=D("5"))
    assert reconcile(inv) == ["total: value could not be checked"]

"""The deterministic rulebook: each check turns an Invoice into severity-tagged Flags.
Sits in the validate stage (read -> extract -> validate -> approval ...); checks advise, they do not decide.
"""
import re
import sqlite3
from decimal import Decimal

from invoice_agent.arithmetic import reconcile
from invoice_agent.config import SCRUTINY_THRESHOLD
from invoice_agent.db import get_inventory
from invoice_agent.models import Flag, Invoice


def _fmt(d: Decimal) -> str:
    """Plain-notation number without trailing zeros (20, not 2E+1)."""
    return format(d.normalize(), "f")


def check_required(inv: Invoice) -> list[Flag]:
    """Reject when vendor or total is missing (sample 1009 has an empty vendor)."""
    flags = []
    if not (inv.vendor or "").strip():
        flags.append(Flag(severity="must_reject", code="missing_vendor", message="Vendor is missing", field_path="vendor"))
    if inv.total is None:
        flags.append(Flag(severity="must_reject", code="missing_total", message="Total is missing", field_path="total"))
    return flags


def check_quantities(inv: Invoice) -> list[Flag]:
    """Reject zero/negative quantities and negative totals (sample 1009)."""
    flags = []
    # Fail closed: an invoice with no lines has nothing to verify against stock or prices.
    if not inv.items:
        flags.append(Flag(severity="must_reject", code="no_line_items", message="Invoice has no line items", field_path="items"))
    for i, it in enumerate(inv.items):
        # A known item with no quantity would silently skip the stock check.
        if it.sku is not None and it.qty is None:
            flags.append(Flag(
                severity="must_reject", code="missing_quantity",
                message=f"{it.name_raw}: quantity is missing", field_path=f"items[{i}].qty",
            ))
        if it.qty is not None and it.qty <= 0:
            flags.append(Flag(
                severity="must_reject", code="non_positive_quantity",
                message=f"{it.name_raw}: quantity {_fmt(it.qty)} is not positive", field_path=f"items[{i}]",
            ))
    if inv.total is not None and inv.total < 0:
        flags.append(Flag(
            severity="must_reject", code="negative_total",
            message=f"Total is negative ({inv.total})", field_path="total",
        ))
    return flags


def check_items(inv: Invoice, conn: sqlite3.Connection) -> list[Flag]:
    """Reject items not in inventory (1008, 1016) and items with zero stock (1003 FakeItem)."""
    flags = []
    for i, it in enumerate(inv.items):
        if it.sku is None:
            msg = f"{it.name_raw}: not in inventory"
            if it.sku_suggestion:
                msg += f" (did you mean {it.sku_suggestion}?)"
            flags.append(Flag(severity="must_reject", code="unknown_item", message=msg, field_path=f"items[{i}]"))
            continue
        inv_row = get_inventory(conn, it.sku)
        # Fail-safe: a SKU that vanished from inventory is treated as unknown, never waved through.
        if inv_row is None:
            flags.append(Flag(severity="must_reject", code="unknown_item",
                              message=f"{it.name_raw}: SKU {it.sku} not found in inventory", field_path=f"items[{i}]"))
        elif inv_row["stock"] == 0:
            flags.append(Flag(
                severity="must_reject", code="zero_stock_item",
                message=f"{it.sku}: zero stock, suspicious item", field_path=f"items[{i}]",
            ))
    return flags


def check_stock(inv: Invoice, conn: sqlite3.Connection) -> list[Flag]:
    """Reject when total quantity per SKU, summed across all its lines, exceeds stock (1013: 15+5+2 of WidgetA)."""
    # sku -> [total qty, line count, index of first line]
    totals: dict[str, list] = {}
    for i, it in enumerate(inv.items):
        # Unknown items are check_items' job; missing quantities cannot be summed.
        if it.sku is None or it.qty is None:
            continue
        entry = totals.setdefault(it.sku, [Decimal(0), 0, i])
        entry[0] += it.qty
        entry[1] += 1
    flags = []
    for sku, (qty, lines, first) in totals.items():
        inv_row = get_inventory(conn, sku)
        if inv_row is not None and qty > inv_row["stock"]:
            flags.append(Flag(
                severity="must_reject", code="exceeds_stock",
                message=f"{sku}: {_fmt(qty)} requested across {lines} line(s), {inv_row['stock']} in stock",
                field_path=f"items[{first}]",
            ))
    return flags


def check_prices(inv: Invoice, conn: sqlite3.Connection) -> list[Flag]:
    """Scrutinize any unit price that differs from the reference price (no tolerance; 1010 rush, 1013 discounts)."""
    flags = []
    for i, it in enumerate(inv.items):
        if it.sku is None:
            continue
        # A known item with no price would silently skip the price check.
        if it.unit_price is None:
            flags.append(Flag(
                severity="needs_scrutiny", code="missing_unit_price",
                message=f"{it.name_raw}: unit price is missing", field_path=f"items[{i}].unit_price",
            ))
            continue
        inv_row = get_inventory(conn, it.sku)
        if inv_row is not None and it.unit_price != inv_row["unit_price"]:
            msg = f"{it.sku}: invoiced at {it.unit_price}, reference price is {inv_row['unit_price']}"
            if it.note:
                msg += f" (line note: {it.note})"
            flags.append(Flag(
                severity="needs_scrutiny", code="price_mismatch", message=msg, field_path=f"items[{i}].unit_price",
            ))
    return flags


def check_arithmetic(inv: Invoice) -> list[Flag]:
    """Scrutinize every line/subtotal/total mismatch reported by reconcile()."""
    return [
        Flag(severity="needs_scrutiny", code="arithmetic_mismatch", message=m, field_path=m.split(":")[0])
        for m in reconcile(inv)
    ]


def check_dates(inv: Invoice) -> list[Flag]:
    """Scrutinize a missing due date or one on/before the invoice date; never compares to today's date."""
    if inv.due_date is None:
        return [Flag(severity="needs_scrutiny", code="due_date_missing",
                     message="Due date is missing or could not be parsed", field_path="due_date")]
    # Only invoice-internal consistency counts: old sample invoices would all be "overdue" against today.
    if inv.date is not None and inv.due_date <= inv.date:
        return [Flag(severity="needs_scrutiny", code="due_before_or_on_invoice_date",
                     message=f"Due date {inv.due_date} is on or before invoice date {inv.date}", field_path="due_date")]
    return []


def check_currency(inv: Invoice) -> list[Flag]:
    """Scrutinize non-USD invoices (1014 EUR); prices are not FX-converted."""
    if inv.currency.upper() != "USD":
        return [Flag(severity="needs_scrutiny", code="non_usd_currency",
                     message=f"Invoice currency is {inv.currency}, not USD", field_path="currency")]
    return []


def check_amount(inv: Invoice) -> list[Flag]:
    """Scrutinize totals above the $10K threshold."""
    if inv.total is not None and inv.total > SCRUTINY_THRESHOLD:
        return [Flag(severity="needs_scrutiny", code="over_10k",
                     message=f"Total {inv.total} exceeds {SCRUTINY_THRESHOLD}", field_path="total")]
    return []


def check_red_flags(inv: Invoice, raw_text: str) -> list[Flag]:
    """Info-only fraud hints from the raw text: pressure language and vendor renames."""
    text = raw_text.lower()
    flags = []
    hits = [w for w in ("urgent", "immediately", "wire transfer") if w in text]
    if hits:
        flags.append(Flag(severity="info", code="urgency_language",
                          message="Urgency/payment-pressure language: " + ", ".join(hits), field_path="notes"))
    if re.search(r"formerly", text):
        flags.append(Flag(severity="info", code="vendor_name_change",
                          message="Text mentions a former vendor name ('formerly')", field_path="vendor"))
    return flags

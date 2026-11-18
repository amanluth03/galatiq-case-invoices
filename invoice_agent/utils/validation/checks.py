"""The deterministic rulebook: each check turns an Invoice into severity-tagged Flags.
Sits in the validate stage (read -> extract -> validate -> approval ...). Messages are plain words with formatted
money and dates, because the review screen, the agents and the rejection reason all show them as written.
"""
import re
import sqlite3
from decimal import Decimal, InvalidOperation

from invoice_agent.utils.arithmetic import line_overcharges, lines_plus_charges, reconcile
from invoice_agent.database.config import SCRUTINY_THRESHOLD
from invoice_agent.database.db import get_inventory
from invoice_agent.models import Flag, Invoice
from invoice_agent.utils.formatting import money, nice_date, quantity


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
                message=f"{it.name_raw} has no quantity", field_path=f"items[{i}].qty",
            ))
        if it.qty is not None and it.qty <= 0:
            flags.append(Flag(
                severity="must_reject", code="non_positive_quantity",
                message=f"{it.name_raw} has a quantity of {quantity(it.qty)}, which isn't positive", field_path=f"items[{i}]",
            ))
    if inv.total is not None and inv.total < 0:
        flags.append(Flag(
            severity="must_reject", code="negative_total",
            message=f"Total of {money(inv.total, inv.currency)} is negative", field_path="total",
        ))
    return flags


def check_items(inv: Invoice, conn: sqlite3.Connection) -> list[Flag]:
    """Reject items not in inventory (1008, 1016) and items with zero stock (1003 FakeItem)."""
    flags = []
    for i, it in enumerate(inv.items):
        if it.sku is None:
            msg = f"{it.name_raw} isn't in inventory"
            if it.sku_suggestion:
                msg += f" (did you mean {it.sku_suggestion}?)"
            flags.append(Flag(severity="must_reject", code="unknown_item", message=msg, field_path=f"items[{i}]"))
            continue
        inv_row = get_inventory(conn, it.sku)
        # Fail-safe: a SKU that vanished from inventory is treated as unknown, never waved through.
        if inv_row is None:
            flags.append(Flag(severity="must_reject", code="unknown_item",
                              message=f"{it.name_raw} ({it.sku}) isn't in inventory", field_path=f"items[{i}]"))
        elif inv_row["stock"] == 0:
            flags.append(Flag(
                severity="must_reject", code="zero_stock_item",
                message=f"{it.sku} has zero stock, a suspicious item", field_path=f"items[{i}]",
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
            # e.g. "Ordered 20 GadgetX, only 5 in stock"; 1013 orders WidgetA on 3 lines, so the line count is said.
            across = f" across {lines} lines" if lines > 1 else ""
            flags.append(Flag(
                severity="must_reject", code="exceeds_stock",
                message=f"Ordered {quantity(qty)} {sku}{across}, only {inv_row['stock']} in stock",
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
                message=f"{it.name_raw} has no unit price", field_path=f"items[{i}].unit_price",
            ))
            continue
        inv_row = get_inventory(conn, it.sku)
        if inv_row is not None and it.unit_price != inv_row["unit_price"]:
            msg = (f"{it.sku} charged {money(it.unit_price, inv.currency)}, "
                   f"Acme's price is {money(inv_row['unit_price'])}")
            if it.note:
                msg += f" (line note: {it.note})"
            flags.append(Flag(
                severity="needs_scrutiny", code="price_mismatch", message=msg, field_path=f"items[{i}].unit_price",
            ))
    return flags


def check_arithmetic(inv: Invoice) -> list[Flag]:
    """Scrutinize every mismatch reported by reconcile(); reject a total higher than what the lines add up to."""
    flags = [
        Flag(severity="needs_scrutiny", code="arithmetic_mismatch", message=m, field_path=m.split(":")[0])
        for m in reconcile(inv)
    ]
    # Overbilling: we would pay more than the goods + tax + shipping are worth (sample 1013, $50 over).
    # Lines are priced as qty x unit_price (not the vendor's line_total) and compared against the lines,
    # not the stated subtotal, so neither an inflated line_total nor an inflated subtotal can hide it.
    # Under-billing (sample 1007) stays an arithmetic_mismatch for the agents to explain.
    try:
        expected = lines_plus_charges(inv)
        overcharged = line_overcharges(inv)
    except InvalidOperation:
        # Fail closed: a total we cannot check against its lines must not be paid.
        if inv.total is not None:
            flags.append(Flag(severity="must_reject", code="total_unverifiable", field_path="total",
                              message="Line amounts are too large to compute, so the total can't be verified"))
        return flags
    for i, stated, computed in overcharged:
        flags.append(Flag(
            severity="must_reject", code="line_total_exceeds_price", field_path=f"items[{i}].line_total",
            message=f"Line {i + 1} total of {money(stated, inv.currency)} is more than quantity × unit price "
                    f"({money(computed, inv.currency)})",
        ))
    if inv.total is not None and expected is not None and inv.total > expected:
        flags.append(Flag(
            severity="must_reject", code="total_exceeds_line_items", field_path="total",
            message=f"Total of {money(inv.total, inv.currency)} is more than the line items plus tax and "
                    f"shipping ({money(expected, inv.currency)})",
        ))
    return flags


def check_dates(inv: Invoice) -> list[Flag]:
    """Scrutinize a missing due date or one on/before the invoice date; never compares to today's date."""
    if inv.due_date is None:
        return [Flag(severity="needs_scrutiny", code="due_date_missing",
                     message="Due date is missing or could not be parsed", field_path="due_date")]
    # Only invoice-internal consistency counts: old sample invoices would all be "overdue" against today.
    if inv.date is not None and inv.due_date <= inv.date:
        return [Flag(severity="needs_scrutiny", code="due_before_or_on_invoice_date",
                     message=f"Due date ({nice_date(inv.due_date)}) is on or before the invoice date", field_path="due_date")]
    return []


def check_currency(inv: Invoice) -> list[Flag]:
    """Scrutinize non-USD invoices (1014 EUR); prices are not FX-converted."""
    if inv.currency.upper() != "USD":
        return [Flag(severity="needs_scrutiny", code="non_usd_currency",
                     message=f"Invoice is in {inv.currency}, not US dollars", field_path="currency")]
    return []


def check_amount(inv: Invoice) -> list[Flag]:
    """Scrutinize totals above the $10K threshold."""
    if inv.total is not None and inv.total > SCRUTINY_THRESHOLD:
        return [Flag(severity="needs_scrutiny", code="over_10k",
                     message=f"Total of {money(inv.total, inv.currency)} is over the "
                             f"{money(SCRUTINY_THRESHOLD).removesuffix('.00')} review threshold", field_path="total")]
    return []


# A number as printed: optional minus and $, digits with thousands commas, optional decimals.
_NUMBER = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")
# A run that starts like a number and may hold OCR look-alikes, e.g. "$3,500.O0" in sample 1012.
_DIGIT_RUN = re.compile(r"[\d$][\dOl,.]*")


def _source_amounts(raw_text: str) -> set[Decimal]:
    """Every number printed in the raw text, to the cent; O->0 and l->1 inside digit runs count as their repair."""
    repaired = _DIGIT_RUN.sub(lambda m: m.group().replace("O", "0").replace("l", "1"), raw_text)
    found = set()
    # Read commas both ways: as thousands separators ("$1,500.00") and as CSV column breaks ("WidgetB,15,500.00").
    for token in _NUMBER.findall(repaired) + _NUMBER.findall(repaired.replace(",", " ")):
        try:
            found.add(Decimal(token.replace("$", "").replace(",", "")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return found


def check_amounts_in_source(inv: Invoice, raw_text: str) -> list[Flag]:
    """Scrutinize any extracted amount that is not printed in the source text (the reader must never rewrite one).
    Catches an LLM "fixing" a vendor's total so the math works (1013 22562.80 -> 22512.80, 1007 15525 -> 15635)."""
    printed = _source_amounts(raw_text)
    fields = [(name, getattr(inv, name)) for name in ("subtotal", "tax", "shipping", "total")]
    for i, it in enumerate(inv.items):
        fields.append((f"items[{i}].unit_price", it.unit_price))
        # A line total the vendor didn't print but that is exactly qty x unit price is derived, not rewritten.
        derived = it.qty is not None and it.unit_price is not None and it.line_total == it.qty * it.unit_price
        if not derived:
            fields.append((f"items[{i}].line_total", it.line_total))
    flags = []
    for path, value in fields:
        # Missing amounts are other checks' job; a zero tax/shipping that isn't printed cannot raise the payment.
        if value is None or value == 0:
            continue
        try:
            in_source = value.quantize(Decimal("0.01")) in printed
        except InvalidOperation:
            in_source = False
        if not in_source:
            flags.append(Flag(severity="needs_scrutiny", code="amount_not_in_source", field_path=path,
                              message=f"{path}: {money(value, inv.currency)} isn't printed anywhere on the invoice"))
    return flags


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
                          message="The invoice mentions a former vendor name (\"formerly\")", field_path="vendor"))
    return flags

"""Tests for the validation rulebook against invoices mirroring the sample files in data/invoices/."""
from datetime import date
from decimal import Decimal as D

import pytest

from invoice_agent.infra import db
from invoice_agent.infra.db import connect, init_db, save_run
from invoice_agent.models import Invoice, LineItem
from invoice_agent.utils.validation.checks import check_dates
from invoice_agent.utils.validation.validate import claim_identity, find_duplicate, fingerprint, run_validation


@pytest.fixture
def conn(tmp_path):
    """Seeded temporary inventory/run-history database."""
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def L(name, qty, price, sku="__same__", note=None, sku_suggestion=None):
    """Build a line item; sku defaults to the name (a known inventory SKU)."""
    return LineItem(name_raw=name, sku=name if sku == "__same__" else sku, qty=D(str(qty)),
                    unit_price=D(str(price)), note=note, sku_suggestion=sku_suggestion)


def inv_1001(**kw):
    """Clean sample 1001."""
    base = dict(invoice_number="INV-1001", vendor="Widgets Inc.", date=date(2026, 1, 15), due_date=date(2026, 2, 1),
                items=[L("WidgetA", 10, "250.00"), L("WidgetB", 5, "500.00")],
                subtotal=D("5000.00"), tax=D("0.00"), total=D("5000.00"))
    base.update(kw)
    return Invoice(**base)


def codes(flags, severity=None):
    """Set of flag codes, optionally for one severity."""
    return {f.code for f in flags if severity is None or f.severity == severity}


def test_1001_clean_has_no_blocking_flags(conn):
    """A clean invoice yields no must_reject or needs_scrutiny flags."""
    flags = run_validation(conn, inv_1001(), "INVOICE ...", [])
    assert not [f for f in flags if f.severity != "info"]


def test_1002_exceeds_stock(conn):
    """GadgetX x20 exceeds stock of 5; also over $10K with due date equal to invoice date."""
    inv = Invoice(invoice_number="1002", vendor="Gadgets Co.", date=date(2026, 1, 30), due_date=date(2026, 1, 30),
                  items=[L("GadgetX", 20, "750")], total=D("15000.00"))
    flags = run_validation(conn, inv, "", [])
    assert "exceeds_stock" in codes(flags, "must_reject")
    assert {"over_10k", "due_before_or_on_invoice_date"} <= codes(flags, "needs_scrutiny")
    msg = next(f.message for f in flags if f.code == "exceeds_stock")
    assert msg == "GadgetX: 20 requested across 1 line(s), 5 in stock"


def test_1003_fake_item(conn):
    """FakeItem has zero stock, so it is rejected, and the urgent wording is an info flag."""
    inv = Invoice(invoice_number="INV-1003", vendor="Fraudster LLC", date=date(2026, 1, 20), due_date=None,
                  items=[L("FakeItem", 100, "1000.00")], total=D("100000.00"))
    raw = "Notes: URGENT - Pay immediately to avoid penalties!!! Wire transfer preferred."
    flags = run_validation(conn, inv, raw, [])
    assert {"zero_stock_item", "exceeds_stock"} <= codes(flags, "must_reject")
    assert "urgency_language" in codes(flags, "info")
    assert "due_date_missing" in codes(flags)


def test_1008_and_1016_unknown_items(conn):
    """Items absent from inventory are must_reject unknown_item."""
    i8 = Invoice(invoice_number="INV-1008", vendor="NoProd Industries", date=date(2026, 1, 10), due_date=date(2026, 1, 20),
                 items=[L("SuperGizmo", 12, "400", sku=None), L("MegaSprocket", 6, "850", sku=None)], total=D("9900.00"))
    i16 = Invoice(invoice_number="INV-1016", vendor="Widgets Inc.", date=date(2026, 1, 27), due_date=date(2026, 2, 27),
                  items=[L("WidgetA", 4, "250"), L("WidgetB", 2, "500"), L("WidgetC", 3, "350", sku=None, sku_suggestion="WidgetA")],
                  subtotal=D("3050.00"), tax=D("183.00"), total=D("3233.00"))
    f8 = [f for f in run_validation(conn, i8, "", []) if f.code == "unknown_item"]
    assert [f.message.split(":")[0] for f in f8] == ["SuperGizmo", "MegaSprocket"]
    f16 = [f for f in run_validation(conn, i16, "", []) if f.code == "unknown_item"]
    assert len(f16) == 1 and f16[0].severity == "must_reject"
    assert "WidgetC" in f16[0].message and "WidgetA" in f16[0].message


def test_1009_data_integrity(conn):
    """Empty vendor, negative quantity and negative total are rejected; missing due date is scrutiny."""
    inv = Invoice(invoice_number="INV-1009", vendor="", date=date(2026, 1, 15), due_date=None,
                  items=[L("WidgetA", -5, "250.00"), L("WidgetB", 2, "500.00")], subtotal=D("1000.00"), total=D("-250.00"))
    flags = run_validation(conn, inv, "", [])
    assert {"missing_vendor", "non_positive_quantity", "negative_total"} <= codes(flags, "must_reject")
    assert "due_date_missing" in codes(flags, "needs_scrutiny")


def test_1013_aggregates_stock(conn):
    """WidgetA across three lines (15+5+2) is aggregated against stock of 15."""
    inv = Invoice(
        invoice_number="INV-1013", vendor="Atlas Industrial Supply", date=date(2026, 1, 24), due_date=date(2026, 3, 24),
        items=[L("WidgetA", 15, "250"), L("WidgetB", 10, "500"), L("GadgetX", 5, "750"),
               L("WidgetA", 5, "240", note="Volume discount"), L("WidgetB", 8, "480", note="Volume discount"),
               L("GadgetX", 3, "750", note="Expedited"), L("WidgetA", 2, "250", note="Replacement"),
               L("GadgetX", 1, "750", note="Sample")],
        subtotal=D("21040.00"), tax=D("1472.80"), total=D("22562.80"))
    flags = run_validation(conn, inv, "", [])
    wa = next(f for f in flags if f.code == "exceeds_stock" and f.message.startswith("WidgetA"))
    assert "22 requested across 3 line(s), 15 in stock" in wa.message
    assert wa.field_path == "items[0]"
    assert "price_mismatch" in codes(flags, "needs_scrutiny")


def test_1010_rush_price_is_scrutiny_not_reject(conn):
    """A $300 rush line is a price_mismatch (scrutiny) mentioning the note, and nothing is must_reject."""
    inv = Invoice(invoice_number="INV-1010", vendor="Consolidated Materials Group", date=date(2026, 1, 27),
                  due_date=date(2026, 2, 26),
                  items=[L("WidgetA", 8, "250"), L("WidgetB", 4, "500"), L("GadgetX", 2, "750"),
                         L("WidgetA", 4, "300", note="rush order")],
                  subtotal=D("6700.00"), tax=D("335.00"), shipping=D("150.00"), total=D("7185.00"))
    flags = run_validation(conn, inv, "", [])
    pm = [f for f in flags if f.code == "price_mismatch"]
    assert len(pm) == 1 and "300" in pm[0].message and "250.00" in pm[0].message and "rush order" in pm[0].message
    assert pm[0].field_path == "items[3].unit_price"
    assert not codes(flags, "must_reject")


def test_1014_eur(conn):
    """A EUR invoice with non-reference prices is flagged for currency and price."""
    inv = Invoice(invoice_number="INV-1014", vendor="TechParts International", date=date(2026, 1, 26),
                  due_date=date(2026, 2, 26), currency="EUR",
                  items=[L("WidgetA", 4, "225.00"), L("WidgetB", 6, "475.00")],
                  subtotal=D("3750.00"), tax=D("375.00"), total=D("4125.00"))
    flags = run_validation(conn, inv, "", [])
    assert {"non_usd_currency", "price_mismatch"} <= codes(flags, "needs_scrutiny")


def test_extraction_issues_become_scrutiny_flags(conn):
    """Each leftover extraction issue is surfaced as an extraction_issue needs_scrutiny flag."""
    flags = run_validation(conn, inv_1001(), "", ["total unreadable"])
    assert [(f.severity, f.code, f.message) for f in flags if f.code == "extraction_issue"] == [
        ("needs_scrutiny", "extraction_issue", "total unreadable")]


def test_never_flags_past_due_against_today(conn):
    """A due date long past today is not flagged: only invoice-internal date consistency is checked."""
    inv = inv_1001(due_date=date(2026, 2, 1))
    flags = run_validation(conn, inv, "", [])
    assert not [f for f in flags if "past_due" in f.code or "overdue" in f.code]
    assert check_dates(inv) == []


def _save(conn, run_id, inv, content_hash):
    """Store a prior run for duplicate-detection tests."""
    save_run(conn, {"run_id": run_id, "invoice": inv, "content_hash": content_hash,
                    "fingerprint": fingerprint(inv), "source_path": run_id})


def _inv_1011(price="250.00", qty="6"):
    """Sample 1011 invoice."""
    return Invoice(invoice_number="INV-1011", vendor="Summit Manufacturing Co.", date=date(2026, 1, 20),
                   due_date=date(2026, 2, 20), items=[L("WidgetA", qty, price), L("WidgetB", 3, "500.00")],
                   total=D("3000.00"))


def test_fingerprint_is_format_independent():
    """Same invoice with differently-scaled numbers and vendor casing fingerprints identically."""
    a = _inv_1011()
    b = _inv_1011(price="250", qty="6.0").model_copy(update={"vendor": " summit manufacturing co. ", "total": D("3000")})
    assert fingerprint(a) == fingerprint(b)


def test_find_duplicate_same_content_other_format(conn):
    """Same number, same fingerprint, different file hash means the second copy is a duplicate."""
    inv = _inv_1011()
    _save(conn, "prior", inv, "hash-pdf")
    res = find_duplicate(conn, inv, fingerprint(inv), "hash-txt", "new")
    assert res.duplicate_of == "prior" and res.flags == []


def test_find_duplicate_reused_number(conn):
    """Same number with different contents is must_reject invoice_number_reused."""
    old = Invoice(invoice_number="INV-1004", vendor="Precision Parts Ltd.", items=[L("WidgetA", 3, "250")], total=D("1890.00"))
    new = Invoice(invoice_number="INV-1004", vendor="Precision Parts Ltd.", items=[L("WidgetA", 4, "250")], total=D("2000.00"))
    _save(conn, "prior", old, "h1")
    res = find_duplicate(conn, new, fingerprint(new), "h2", "new")
    assert res.duplicate_of is None
    assert [(f.severity, f.code) for f in res.flags] == [("must_reject", "invoice_number_reused")]
    assert "INV-1004 was already processed with different contents" in res.flags[0].message


def test_find_duplicate_ignores_reprocessed_same_file(conn):
    """Re-processing the same file (same run_id) is neither a duplicate nor a reuse."""
    inv = _inv_1011()
    _save(conn, "r1", inv, "same-hash")
    res = find_duplicate(conn, inv, fingerprint(inv), "same-hash", "r1")
    assert res.duplicate_of is None and res.flags == []


def test_find_duplicate_same_hash_other_run_is_duplicate(conn):
    """A prior run with the same content hash but a different run_id is a duplicate, not ignored."""
    inv = _inv_1011()
    _save(conn, "r1", inv, "h")
    res = find_duplicate(conn, inv, fingerprint(inv), "h", "r2")
    assert res.duplicate_of == "r1"


def test_missing_quantity_rejects(conn):
    """A known item with no quantity is must_reject missing_quantity."""
    inv = inv_1001()
    inv.items[0].qty = None
    f = [f for f in run_validation(conn, inv, "", []) if f.code == "missing_quantity"]
    assert [(x.severity, x.field_path) for x in f] == [("must_reject", "items[0].qty")]


def test_missing_unit_price_needs_scrutiny(conn):
    """A known item with no unit price is needs_scrutiny missing_unit_price."""
    inv = inv_1001()
    inv.items[1].unit_price = None
    f = [f for f in run_validation(conn, inv, "", []) if f.code == "missing_unit_price"]
    assert [(x.severity, x.field_path) for x in f] == [("needs_scrutiny", "items[1].unit_price")]


def test_no_line_items_rejects(conn):
    """An invoice with zero lines is must_reject no_line_items."""
    f = [f for f in run_validation(conn, inv_1001(items=[]), "", []) if f.code == "no_line_items"]
    assert [(x.severity, x.field_path) for x in f] == [("must_reject", "items")]


def test_sku_missing_from_inventory_rejects(conn):
    """A SKU set on the line but absent from inventory fails safe as unknown_item."""
    inv = inv_1001(items=[L("Ghost", 1, "10")])
    assert "unknown_item" in codes(run_validation(conn, inv, "", []), "must_reject")


def test_find_duplicate_without_invoice_number(conn):
    """No invoice number means nothing to compare against."""
    inv = Invoice(vendor="X", total=D("1"))
    res = find_duplicate(conn, inv, fingerprint(inv), "h", "new")
    assert res.duplicate_of is None and res.flags == []


def inv_1013(**kw):
    """Sample 1013 (8 lines, subtotal 21040.00, tax 1472.80): stated total 22562.80, but lines + tax = 22512.80."""
    base = dict(invoice_number="INV-1013", vendor="Atlas Industrial Supply", date=date(2026, 1, 24),
                due_date=date(2026, 3, 24),
                items=[L("WidgetA", 15, "250.00"), L("WidgetB", 10, "500.00"), L("GadgetX", 5, "750.00"),
                       L("WidgetA", 5, "240.00"), L("WidgetB", 8, "480.00"), L("GadgetX", 3, "750.00"),
                       L("WidgetA", 2, "250.00"), L("GadgetX", 1, "750.00")],
                subtotal=D("21040.00"), tax=D("1472.80"), total=D("22562.80"))
    base.update(kw)
    return Invoice(**base)


def inv_1007(**kw):
    """Sample 1007: lines 14750.00 + tax 885.00 = 15635.00, but the stated total is 15525.00 (under-billed)."""
    base = dict(invoice_number="INV-1007", vendor="MegaWidgets Corp", date=date(2026, 1, 28), due_date=date(2026, 2, 28),
                items=[L("WidgetA", 20, "250.00"), L("WidgetB", 15, "500.00"), L("GadgetX", 3, "750.00")],
                subtotal=D("14750.00"), tax=D("885.00"), total=D("15525.00"))
    base.update(kw)
    return Invoice(**base)


def test_overbilled_total_is_must_reject(conn):
    """1013's total is $50 more than its lines + tax: must_reject total_exceeds_line_items (plus the arithmetic flag)."""
    flags = run_validation(conn, inv_1013(), "", [])
    over = [f for f in flags if f.code == "total_exceeds_line_items"]
    assert len(over) == 1 and over[0].severity == "must_reject" and over[0].field_path == "total"
    assert "22562.80" in over[0].message and "22512.80" in over[0].message
    assert "arithmetic_mismatch" in codes(flags, "needs_scrutiny")


def test_underbilled_total_is_only_scrutiny(conn):
    """1007's total is below its lines + tax: still arithmetic_mismatch (needs_scrutiny), never total_exceeds_line_items."""
    flags = run_validation(conn, inv_1007(), "", [])
    assert "total_exceeds_line_items" not in codes(flags)
    assert "arithmetic_mismatch" in codes(flags, "needs_scrutiny")


def test_overbilling_uses_line_amounts_not_stated_subtotal(conn):
    """An inflated subtotal cannot hide overbilling: the gate compares against the line amounts themselves."""
    flags = run_validation(conn, inv_1001(subtotal=D("5100.00"), total=D("5100.00")), "", [])
    assert "total_exceeds_line_items" in codes(flags, "must_reject")


def test_clean_total_has_no_overbilling_flag(conn):
    """1001 reconciles exactly, so no total_exceeds_line_items flag."""
    assert "total_exceeds_line_items" not in codes(run_validation(conn, inv_1001(), "", []))


def test_inflated_line_total_is_must_reject_and_caught_by_total_gate(conn):
    """A vendor inflating a line_total (10 x 250 billed as 2600) cannot slip past: both gates reject."""
    items = [L("WidgetA", 10, "250.00"), L("WidgetB", 5, "500.00")]
    items[0] = items[0].model_copy(update={"line_total": D("2600.00")})
    flags = run_validation(conn, inv_1001(items=items, subtotal=D("5100.00"), total=D("5100.00")), "", [])
    line = [f for f in flags if f.code == "line_total_exceeds_price"]
    assert len(line) == 1 and line[0].severity == "must_reject" and line[0].field_path == "items[0].line_total"
    assert "2600.00" in line[0].message and "2500.00" in line[0].message
    # The total gate uses qty x unit_price, so the inflated line does not raise the expected total.
    assert "total_exceeds_line_items" in codes(flags, "must_reject")


def test_line_total_below_price_is_not_line_overcharge(conn):
    """A line_total under qty x unit_price is not overbilling: only the existing arithmetic scrutiny applies."""
    items = [L("WidgetA", 10, "250.00"), L("WidgetB", 5, "500.00")]
    items[0] = items[0].model_copy(update={"line_total": D("2400.00")})
    flags = run_validation(conn, inv_1001(items=items), "", [])
    assert "line_total_exceeds_price" not in codes(flags)
    assert "arithmetic_mismatch" in codes(flags, "needs_scrutiny")


def test_line_total_used_when_price_missing(conn):
    """With no unit price, the stated line_total is the only amount, so the total gate falls back to it."""
    items = [L("WidgetA", 10, "250.00"), L("WidgetB", 5, "500.00")]
    items[1] = items[1].model_copy(update={"unit_price": None, "line_total": D("2500.00")})
    assert "total_exceeds_line_items" not in codes(run_validation(conn, inv_1001(items=items), "", []))
    flags = run_validation(conn, inv_1001(items=items, subtotal=D("5001.00"), total=D("5001.00")), "", [])
    assert "total_exceeds_line_items" in codes(flags, "must_reject")


def test_uncomputable_amounts_fail_closed(conn):
    """If the line amounts overflow the arithmetic but a total is stated, the invoice is must_reject total_unverifiable."""
    items = [L("WidgetA", "1E+30", "250.00")]
    flags = run_validation(conn, inv_1001(items=items, subtotal=None), "", [])
    unverifiable = [f for f in flags if f.code == "total_unverifiable"]
    assert len(unverifiable) == 1 and unverifiable[0].severity == "must_reject"


def test_find_duplicate_ignores_duplicate_rows(conn):
    """A run already marked DUPLICATE is never treated as the original, so re-running the real original keeps it original."""
    inv = _inv_1011()
    _save(conn, "copy", inv, "hash-txt")
    conn.execute("UPDATE runs SET status = 'DUPLICATE' WHERE run_id = 'copy'")
    res = find_duplicate(conn, inv, fingerprint(inv), "hash-pdf", "original")
    assert res.duplicate_of is None and res.flags == []


def test_claim_identity_records_pending_row(conn):
    """claim_identity records the run's invoice number and fingerprint at once, so a later lookup sees it as the original."""
    inv = _inv_1011()
    first = claim_identity(conn, inv, fingerprint(inv), "hash-pdf", "r1", "a.pdf")
    second = claim_identity(conn, inv, fingerprint(inv), "hash-txt", "r2", "a.txt")
    assert first.duplicate_of is None and second.duplicate_of == "r1"
    row = db.get_run(conn, "r1")
    assert (row["invoice_number"], row["fingerprint"], row["content_hash"]) == ("INV-1011", fingerprint(inv), "hash-pdf")

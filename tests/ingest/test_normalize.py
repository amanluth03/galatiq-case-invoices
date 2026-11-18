from datetime import date
from decimal import Decimal

import pytest

from invoice_agent.ingest.normalize import (
    match_sku,
    normalize_currency,
    normalize_invoice_number,
    parse_date,
    parse_money,
    to_invoice,
)
from invoice_agent.models import ExtractedField, ExtractedInvoice, ExtractedLineItem


def F(value, raw=None, confidence="high"):
    return ExtractedField(value=value, raw=raw if raw is not None else value, confidence=confidence)


@pytest.mark.parametrize("s,expected", [
    ("Jan 30 2026", date(2026, 1, 30)), ("26-Jan-2026", date(2026, 1, 26)),
    ("01/28/2026", date(2026, 1, 28)), ("2026-02-01", date(2026, 2, 1)),
    ("yesterday", None), (None, None)])
def test_parse_date(s, expected):
    assert parse_date(s) == expected


def test_parse_money():
    assert parse_money("$3,500.00") == Decimal("3500.00") and parse_money("abc") is None
    assert parse_money("-250.00") == Decimal("-250.00")
    assert parse_money("12") == Decimal("12")
    assert parse_money(None) is None


@pytest.mark.parametrize("s", ["1002", "INV 1002", "#INV-1002", "INV-1002"])
def test_invoice_number(s):
    assert normalize_invoice_number(s) == "INV-1002"


def test_invoice_number_edge():
    assert normalize_invoice_number("ABC") == "ABC"
    assert normalize_invoice_number("  ") is None
    assert normalize_invoice_number(None) is None


def test_match_sku():
    skus = ["WidgetA", "WidgetB", "GadgetX", "FakeItem"]
    assert match_sku("Widget A", skus) == ("WidgetA", None)
    assert match_sku("WidgetA (rush order)", skus) == ("WidgetA", None)
    assert match_sku("WidgetC", skus)[0] is None
    assert match_sku("SuperGizmo", skus) == (None, None)


def test_match_sku_fuzzy():
    assert match_sku("Widgget A", ["WidgetA", "GadgetX"]) == (None, "WidgetA")


def test_normalize_currency():
    assert normalize_currency("$") == ("USD", False)
    assert normalize_currency("eur") == ("EUR", False)
    assert normalize_currency("€") == ("EUR", False)
    assert normalize_currency(None) == ("USD", True)


def test_to_invoice_records_uncertainty_for_corrected_value():
    ext = ExtractedInvoice(
        invoice_number=F("INV-1"), currency=F("USD"),
        date=F("2026-01-26", "26-Jan-2O26", "medium"))
    inv, unc = to_invoice(ext, [])
    assert inv.date == date(2026, 1, 26)
    assert len(unc) == 1
    assert unc[0].field_path == "date" and unc[0].raw == "26-Jan-2O26" and unc[0].confidence == "medium"


def test_to_invoice_unparseable_due_date():
    ext = ExtractedInvoice(currency=F("USD"), due_date=F("yesterday"))
    inv, unc = to_invoice(ext, [])
    assert inv.due_date is None
    assert any(u.field_path == "due_date" and u.reason == "could not parse" for u in unc)


def test_to_invoice_items_and_currency_default():
    ext = ExtractedInvoice(items=[
        ExtractedLineItem(name=F("Widget A"), qty=F("2"), unit_price=F("$5.00", confidence="low")),
        ExtractedLineItem(name=F("Widgget B"), qty=F("x"), unit_price=F("1")),
    ])
    inv, unc = to_invoice(ext, ["WidgetA", "WidgetB"])
    assert inv.currency == "USD"
    assert inv.items[0].sku == "WidgetA" and inv.items[0].unit_price == Decimal("5.00")
    assert inv.items[1].sku is None and inv.items[1].sku_suggestion == "WidgetB"
    assert inv.items[1].qty is None
    by = {u.field_path: u for u in unc}
    assert by["currency"].confidence == "low"
    assert by["items[0].unit_price"].confidence == "low"
    assert by["items[1].qty"].reason == "could not parse"
    assert by["items[1].name"].confidence == "low" and by["items[1].name"].normalized == "WidgetB"


@pytest.mark.parametrize("s", ["1e30", "1E+999999", "1000000000000", "1.23456", "NaN", "Infinity"])
def test_parse_money_rejects_hostile(s):
    assert parse_money(s) is None


def test_parse_money_negative_and_limits():
    assert parse_money("-250.00") == Decimal("-250.00")
    assert parse_money("999999999999.9999") == Decimal("999999999999.9999")


@pytest.mark.parametrize("s", ["Jan 2026", "5", "Jan 30"])
def test_parse_date_requires_full_date(s):
    assert parse_date(s) is None


def _name_unc(name):
    ext = ExtractedInvoice(currency=F("USD"), items=[
        ExtractedLineItem(name=F(name), qty=F("1"), unit_price=F("1"))])
    inv, unc = to_invoice(ext, ["WidgetA"])
    return inv, [u for u in unc if u.field_path == "items[0].name"]


def test_to_invoice_flags_removed_parenthetical():
    inv, unc = _name_unc("WidgetA (rush order)")
    assert inv.items[0].sku == "WidgetA"
    assert len(unc) == 1 and unc[0].confidence == "medium"
    assert "rush order" in unc[0].reason and unc[0].raw == "WidgetA (rush order)"
    assert unc[0].normalized == "WidgetA"


def test_to_invoice_whitespace_difference_not_flagged():
    inv, unc = _name_unc("Widget A")
    assert inv.items[0].sku == "WidgetA" and unc == []


def test_to_invoice_nfkc_name():
    inv, unc = _name_unc("ＷidgetＡ")
    assert inv.items[0].sku == "WidgetA"

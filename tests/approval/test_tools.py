"""Tests for the approval policy text, checklist check, and the agent tools."""
import json

import pytest

from invoice_agent.approval.policy import APPROVAL_POLICY, CHECKLIST_KEYS, checklist_complete
from invoice_agent.approval.tools import ANALYST_TOOLS, VP_TOOLS, make_tools
from invoice_agent.db import connect, init_db, save_run
from invoice_agent.models import Invoice, Recommendation

FULL = {k: "ok" for k in CHECKLIST_KEYS}


@pytest.fixture
def conn(tmp_path):
    """Seeded temporary database."""
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def rec(checklist):
    """Build a recommendation carrying the given checklist."""
    return Recommendation(decision="REJECT", reasoning="r", checklist=checklist)


def test_checklist_complete_requires_all_keys():
    """Missing or blank checklist items make the checklist incomplete."""
    assert checklist_complete(rec(FULL))
    assert not checklist_complete(rec({k: v for k, v in FULL.items() if k != "arithmetic"}))
    assert not checklist_complete(rec({**FULL, "price_check": "  "}))
    assert not checklist_complete(rec(None))


def test_policy_mentions_every_flag_code_and_checklist_key():
    """The policy text the agents read covers every rulebook code and checklist key."""
    codes = """exceeds_stock unknown_item zero_stock_item non_positive_quantity negative_total missing_vendor
    missing_total missing_quantity no_line_items invoice_number_reused price_mismatch arithmetic_mismatch over_10k
    due_date_missing due_before_or_on_invoice_date non_usd_currency missing_unit_price extraction_issue
    urgency_language vendor_name_change""".split()
    for code in codes + list(CHECKLIST_KEYS):
        assert code in APPROVAL_POLICY
    assert "well-argued REJECT is a good outcome" in APPROVAL_POLICY


def test_tool_names(conn):
    """make_tools exposes every analyst and VP tool name."""
    tools = make_tools(conn)
    assert set(ANALYST_TOOLS) <= set(tools) and set(VP_TOOLS) <= set(tools)


def test_calculate(conn):
    """The calculator does arithmetic and rejects anything else instead of evaluating it."""
    calc = make_tools(conn)["calculate"]
    assert calc.invoke({"expression": "15*250 + 5*240"}) == "4950"
    assert calc.invoke({"expression": "__import__('os')"}).startswith("error")
    assert calc.invoke({"expression": "1/0"}).startswith("error")
    assert calc.invoke({"expression": "2**3"}).startswith("error")


def test_get_inventory_tool(conn):
    """Known SKUs return stock and price; unknown SKUs return a not-found message."""
    inv = make_tools(conn)["get_inventory"]
    assert json.loads(inv.invoke({"item": "WidgetA"}))["stock"] == 15
    assert "not found" in inv.invoke({"item": "WidgetC"})


def test_get_invoice_history_by_vendor(conn):
    """History filters by vendor case-insensitively and by invoice number, hiding state_json."""
    save_run(conn, {"run_id": "r1", "invoice": Invoice(invoice_number="INV-1001", vendor="Widgets Inc.", total="5000")})
    hist = make_tools(conn)["get_invoice_history"]
    rows = json.loads(hist.invoke({"vendor": "widgets inc."}))
    assert rows[0]["invoice_number"] == "INV-1001" and "state" not in rows[0]
    assert json.loads(hist.invoke({"invoice_number": "1001"}))[0]["vendor"] == "Widgets Inc."
    assert hist.invoke({"vendor": "Nobody"}) == "no prior invoices found"

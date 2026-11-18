"""Tests for the review screen (queue, detail, decision panel) via FastAPI's TestClient, with fake agents and a tmp database."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from invoice_agent.infra import db
from invoice_agent.models import FinalOutcome, Flag, Invoice, LineItem
from invoice_agent.runner import process_file
from invoice_agent.web.app import create_app
from tests.fakes import make_extracted
from tests.test_graph import extracted_1002
from tests.test_runner import INVOICES, build


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded app database for each test."""
    c = db.connect(tmp_path / "app.db")
    db.init_db(c)
    yield c
    c.close()


def seed(conn, run_id, status, number, total, flags=()):
    """Save a finished run row directly, as if the pipeline had produced it."""
    inv = Invoice(invoice_number=number, vendor="Widgets Inc.", total=Decimal(total),
                  items=[LineItem(name_raw="WidgetA", sku="WidgetA", qty=Decimal(1), unit_price=Decimal(total))])
    db.save_run(conn, {"run_id": run_id, "source_path": f"data/invoices/{number}.txt", "invoice": inv,
                       "flags": list(flags), "raw_text": f"Invoice {number}",
                       "final": FinalOutcome(status=status, decided_by="agents", reasoning=f"reason for {number}")})


def client_for(conn, extractions, **kw):
    """A TestClient over the app wired to fake deps/graph; returns (client, deps, pay fake)."""
    deps, graph, pay = build(conn, extractions, **kw)
    # follow_redirects=False so tests can see the 303 itself.
    return TestClient(create_app(deps, graph, conn), follow_redirects=False), deps, graph, pay


def test_queue_lists_needs_review_first(conn):
    """The queue shows awaiting-review invoices above newer finished ones, with dollar totals in the header."""
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "12000.00")
    seed(conn, "p1", "PAID", "INV-PAID", "5000.00")
    seed(conn, "x1", "REJECTED", "INV-REJ", "750.00",
         [Flag(severity="must_reject", code="unknown_item", message="Gizmo: not in inventory", field_path="items[0]")])
    client, *_ = client_for(conn, [])
    html = client.get("/").text
    # The review run is the oldest, yet listed first; finished runs follow newest first.
    assert html.index("INV-REVIEW") < html.index("INV-REJ") < html.index("INV-PAID")
    assert "$5,000.00 paid" in html and "$750.00 rejected" in html
    assert "1 awaiting your review ($12,000.00)" in html
    assert "Needs your review" in html and "Blocked: 1 item isn&#39;t in inventory" in html


def test_detail_shows_plain_verdict_and_no_raw_json(conn):
    """A run the agents could not settle shows a plain-English verdict, the source text and no raw JSON."""
    client, deps, graph, _ = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resp = client.get(f"/invoices/{state['run_id']}")
    assert resp.status_code == 200
    assert "Agents couldn&#39;t agree after 3 rounds — your call" in resp.text
    assert "Widgets Inc." in resp.text and "$5,000.00" in resp.text
    assert '{"' not in resp.text
    # Decision panel is shown only for runs awaiting review.
    assert 'name="decision"' in resp.text


def test_detail_unknown_run_is_404(conn):
    """A run id that does not exist gives a 404 page, not a crash."""
    client, *_ = client_for(conn, [])
    assert client.get("/invoices/nope").status_code == 404


def test_decision_requires_justification_over_blocking_flags(conn):
    """Approving over a must_reject flag with a blank justification is refused on-page (400), keeps the typed note and pays nothing."""
    client, deps, graph, pay = client_for(conn, [extracted_1002()], send_back=True)
    state = process_file(INVOICES / "invoice_1002.txt", deps, graph)
    assert state["final"].status == "NEEDS_HUMAN_REVIEW"
    resp = client.post(f"/invoices/{state['run_id']}/decision",
                       data={"decision": "APPROVE", "note": "my typed note", "justification": "  "})
    assert resp.status_code == 400
    assert "justification" in resp.text and "my typed note" in resp.text
    assert db.get_run(conn, state["run_id"])["status"] == "NEEDS_HUMAN_REVIEW" and not pay.calls


def test_decision_approve_resumes_and_redirects(conn):
    """A human APPROVE on a paused run resumes the graph, pays, and redirects back to the detail page."""
    client, deps, graph, pay = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resp = client.post(f"/invoices/{state['run_id']}/decision",
                       data={"decision": "APPROVE", "note": "vendor confirmed", "justification": ""})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/invoices/{state['run_id']}")
    assert db.get_run(conn, state["run_id"])["status"] == "PAID" and len(pay.calls) == 1


def test_decision_on_finished_run_is_409(conn):
    """Deciding on a run that is not awaiting review (here: already paid) is refused with 409."""
    client, deps, graph, pay = client_for(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resp = client.post(f"/invoices/{state['run_id']}/decision", data={"decision": "APPROVE", "note": ""})
    assert resp.status_code == 409 and len(pay.calls) == 1

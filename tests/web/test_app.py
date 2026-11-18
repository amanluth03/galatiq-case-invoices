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

# TestClient serves as http://testserver, so this is what a post from our own page carries.
SAME_ORIGIN = "http://testserver"


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
    # follow_redirects=False so tests can see the 303 itself; the Origin header makes posts look like our own form.
    client = TestClient(create_app(deps, graph, conn), follow_redirects=False, headers={"origin": SAME_ORIGIN})
    return client, deps, graph, pay


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
    assert '{"' not in resp.text and '{&#34;' not in resp.text
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


def test_queue_error_row_says_processing_failed_and_times_are_utc(conn):
    """An ERROR after the file was read reads "Processing failed: <reason>", and queue times are labelled UTC."""
    seed(conn, "e1", "ERROR", "INV-ERR", "100.00")
    client, *_ = client_for(conn, [])
    html = client.get("/").text
    assert "Processing failed: reason for INV-ERR" in html and " UTC</td>" in html


def test_cross_origin_decision_is_403_and_pays_nothing(conn):
    """A decision posted from another site (CSRF) is refused with a plain 403 page; the run stays paused, unpaid."""
    client, deps, graph, pay = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resp = client.post(f"/invoices/{state['run_id']}/decision", data={"decision": "APPROVE", "note": ""},
                       headers={"origin": "http://evil.example"})
    assert resp.status_code == 403 and "Nothing was approved, rejected or paid" in resp.text
    assert db.get_run(conn, state["run_id"])["status"] == "NEEDS_HUMAN_REVIEW" and not pay.calls


def test_decision_without_origin_or_referer_is_403(conn):
    """A post that says nothing about where it came from is refused (fail closed)."""
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "100.00")
    client, *_ = client_for(conn, [])
    client.headers.pop("origin")
    assert client.post("/invoices/r1/decision", data={"decision": "REJECT"}).status_code == 403


def test_same_origin_referer_decision_works(conn):
    """Without an Origin header, a Referer from our own detail page is accepted and the decision goes through."""
    client, deps, graph, pay = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    client.headers.pop("origin")
    resp = client.post(f"/invoices/{state['run_id']}/decision", data={"decision": "REJECT", "note": "no PO"},
                       headers={"referer": f"{SAME_ORIGIN}/invoices/{state['run_id']}"})
    assert resp.status_code == 303 and db.get_run(conn, state["run_id"])["status"] == "REJECTED"


def test_unknown_host_is_400(conn):
    """A request whose Host isn't localhost (e.g. via DNS rebinding) is refused with 400."""
    client, *_ = client_for(conn, [])
    assert client.get("/", headers={"host": "evil.example"}).status_code == 400


def test_decision_with_broken_checkpoint_shows_plain_500(conn):
    """If resuming fails (missing or damaged checkpoint), the reviewer gets a plain notice and a 500, not a stack trace."""
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "100.00")
    deps, _, pay = build(conn, [])

    class BrokenGraph:
        """Graph stand-in whose resume always fails, as with a lost checkpoint."""

        def invoke(self, *a, **k):
            """Fail as a corrupt checkpoint would."""
            raise RuntimeError("checkpoint gone")

    client = TestClient(create_app(deps, BrokenGraph(), conn), headers={"origin": SAME_ORIGIN})
    resp = client.post("/invoices/r1/decision", data={"decision": "REJECT", "note": "keep me"})
    assert resp.status_code == 500 and "saved progress is missing or damaged" in resp.text
    assert "checkpoint gone" not in resp.text and "keep me" in resp.text and not pay.calls


def test_decision_on_already_paid_checkpoint_does_not_resume(conn):
    """A run whose checkpoint already ended PAID (crash before the final save) is shown as paid, never paid again."""
    client, deps, graph, pay = client_for(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert state["final"].status == "PAID" and len(pay.calls) == 1
    # The row still looks paused, as if the save after payment never happened.
    seed(conn, state["run_id"], "NEEDS_HUMAN_REVIEW", "INV-1001", "5000.00")
    resp = client.post(f"/invoices/{state['run_id']}/decision", data={"decision": "APPROVE", "note": ""})
    assert "already been paid" in resp.text and len(pay.calls) == 1
    assert db.get_run(conn, state["run_id"])["status"] == "PAID"


def test_concurrent_status_requests_do_not_error(conn):
    """Many /status polls from parallel threads (live.js on several tabs) all succeed: each request has its own connection."""
    from concurrent.futures import ThreadPoolExecutor

    seed(conn, "r1", "PAID", "INV-1", "100.00")
    client, *_ = client_for(conn, [])
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.get("/invoices/r1/status").status_code, range(64)))
    assert codes == [200] * 64

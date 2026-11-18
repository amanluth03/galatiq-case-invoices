"""Tests for the review screen (home dashboard, invoice page with its story and details, Invoices sidebar, decision
panel, status JSON, cache-safe static files) via FastAPI's TestClient, with fake agents and a tmp database."""
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


def test_home_dashboard_shows_tiles_and_waiting_first(conn):
    """Home is the drop card plus a dashboard: tiles with counts and dollar totals, then every invoice with the
    one waiting for a person first (finished ones newest first). It never shows a story inline."""
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "12000.00")
    seed(conn, "p1", "PAID", "INV-PAID", "5000.00")
    seed(conn, "x1", "REJECTED", "INV-REJ", "750.00",
         [Flag(severity="must_reject", code="unknown_item", message="Gizmo isn't in inventory", field_path="items[0]")])
    client, *_ = client_for(conn, [])
    html = client.get("/").text
    board = html[html.index("data-dashboard"):]
    # The review run is the oldest, yet listed first; finished runs follow newest first.
    assert board.index("INV-REVIEW.txt") < board.index("INV-REJ.txt") < board.index("INV-PAID.txt")
    assert "Waiting for you" in board and ">Paid<" in board and ">Rejected<" in board
    assert "$12,000.00 at stake" in board and "tile-hot" in board and "$5,000.00" in board and "$750.00" in board
    assert "Hard rule: 1 item isn&#39;t in inventory" in board and 'href="/invoices/r1"' in board
    assert "data-live-steps" not in html and "data-live-run" not in html


def test_invoice_page_has_sidebar_and_way_home(conn):
    """An invoice page keeps the Invoices list beside its story (this one marked current) and links back home."""
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "12000.00")
    seed(conn, "p1", "PAID", "INV-PAID", "5000.00")
    client, *_ = client_for(conn, [])
    html = client.get("/invoices/p1").text
    runs = html[html.index('class="runs"'):]
    assert runs.index("INV-REVIEW.txt") < runs.index("INV-PAID.txt")
    assert 'href="/invoices/p1" aria-current="page"' in runs
    assert "Process another invoice" in html and 'action="/upload"' not in html


def test_empty_main_page_shows_drop_zone_and_hint(conn):
    """A fresh start shows the drop card, the samples button and a hint which file to try, and no tiles yet."""
    client, *_ = client_for(conn, [])
    html = client.get("/").text
    assert "Drop an invoice here, or choose a file" in html and 'type="file"' in html
    assert "Run all 20 sample invoices" in html and "invoice_1010.txt" in html
    assert 'class="tiles"' not in html and "data-live-run" not in html


def test_story_shows_plain_steps_and_no_raw_json(conn):
    """A run the agents could not settle tells its story in plain steps, with the decision panel inline."""
    client, deps, graph, _ = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resp = client.get(f"/invoices/{state['run_id']}")
    assert resp.status_code == 200
    html = resp.text
    for title in ("1. Read the file", "2. Ingestion agent read it", "3. Checked against inventory and rules",
                  "4. Analyst agent", "5. VP agent reviews", "6. Guardrail", "7. Decision: Waiting for you"):
        assert title in html
    assert "the agents couldn&#39;t agree after 3 rounds" in html and "Round 3" in html
    assert "Widgets Inc., $5,000.00" in html and "Invoice details" in html and "Original document" in html
    assert '{"' not in html and '{&#34;' not in html and "NEEDS_HUMAN_REVIEW" not in html
    # Decision panel is shown only for runs waiting for a person.
    assert 'name="decision"' in html and "Approve and pay $5,000.00" in html
    assert 'aria-live="polite"' in html and "data-live-run" not in html


def test_status_json_is_the_story(conn):
    """/status returns the status, plain pill label, finished flag and the same 7 steps the page shows."""
    client, deps, graph, _ = client_for(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    s = client.get(f"/invoices/{state['run_id']}/status").json()
    assert s["status"] == "PAID" and s["label"] == "Paid" and s["finished"] is True
    assert s["file_name"] == "invoice_1001.txt"
    assert [step["number"] for step in s["steps"]] == [1, 2, 3, 4, 5, 6, 7]
    assert set(s["steps"][0]) == {"number", "title", "state", "lines"}
    assert set(s["steps"][1]["lines"][0]) == {"kind", "text"}
    assert s["steps"][6]["title"] == "Decision: Paid"


def test_vendor_name_with_script_is_escaped(conn):
    """A vendor name carrying HTML is shown as text on the page, never run as a script."""
    client, deps, graph, _ = client_for(conn, [make_extracted(vendor="<script>alert(1)</script>")])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    html = client.get(f"/invoices/{state['run_id']}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html


def test_detail_unknown_run_is_404(conn):
    """A run id that does not exist gives a 404 page, not a crash."""
    client, *_ = client_for(conn, [])
    assert client.get("/invoices/nope").status_code == 404


def test_decision_requires_justification_over_blocking_flags(conn):
    """Approving over a must_reject flag with a blank justification is refused on-page (400), keeps the typed note and pays nothing."""
    # A run paused before hard-rule invoices skipped the agents can still wait for a person with a blocking flag.
    seed(conn, "r1", "NEEDS_HUMAN_REVIEW", "INV-REVIEW", "100.00",
         [Flag(severity="must_reject", code="unknown_item", message="Gizmo isn't in inventory", field_path="items[0]")])
    client, deps, graph, pay = client_for(conn, [])
    resp = client.post("/invoices/r1/decision",
                       data={"decision": "APPROVE", "note": "my typed note", "justification": "  "})
    assert resp.status_code == 400
    assert "justification" in resp.text and "my typed note" in resp.text
    assert db.get_run(conn, "r1")["status"] == "NEEDS_HUMAN_REVIEW" and not pay.calls


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


def test_error_story_names_the_failed_step_and_times_are_utc(conn):
    """An ERROR after the file was read marks the failing step with its reason, and the decision time says UTC."""
    seed(conn, "e1", "ERROR", "INV-ERR", "100.00")
    client, *_ = client_for(conn, [])
    html = client.get("/invoices/e1").text
    assert "st-failed" in html and "Reason for INV-ERR" in html and " UTC</p>" in html
    assert "nothing was paid" in html


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


def test_invoice_details_show_fields_flags_and_the_full_exchange(conn):
    """Under the story, "Invoice details" shows the original document beside the extracted fields and line items,
    flags grouped by severity, and every analyst/VP round with complete reasoning (collapsible)."""
    client, deps, graph, _ = client_for(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    html = client.get(f"/invoices/{state['run_id']}").text
    details = html[html.index('id="details-title"'):]
    assert "Original document" in details and 'class="doc"' in details
    assert "What the reader extracted" in details and "Invoice number" in details and "Line items" in details
    assert "WidgetA" in details and "Unit price" in details
    assert "What the checks found" in details
    assert '<details class="card timeline">' in details and "Round 3" in details and "sent it back" in details


def test_rule_rejected_invoice_page_shows_flags_and_no_rounds(conn):
    """A hard-rule rejection's page lists the blocking flag in the details and has no agents' exchange."""
    client, deps, graph, _ = client_for(conn, [extracted_1002()])
    state = process_file(INVOICES / "invoice_1002.txt", deps, graph)
    html = client.get(f"/invoices/{state['run_id']}").text
    assert ">Blocking<" in html and "Ordered 20 GadgetX, only 5 in stock" in html
    assert "the agents weren&#39;t needed" in html and "How the agents decided" not in html


def test_static_urls_are_versioned_and_revalidated(conn):
    """Every static URL carries a content hash, and static files are served with Cache-Control: no-cache,
    so a browser never pairs a new page with an old cached stylesheet."""
    from invoice_agent.web.app import static_versions
    client, *_ = client_for(conn, [])
    html = client.get("/").text
    versions = static_versions()
    assert f'/static/styles.css?v={versions["styles.css"]}"' in html
    assert f'/static/live.js?v={versions["live.js"]}"' in html
    resp = client.get(f"/static/styles.css?v={versions['styles.css']}")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache"


def test_page_reads_right_without_css(conn):
    """Without the stylesheet the page still reads cleanly: the native file input is hidden by inline styles and
    step numbers come only from the text (no <ol> or bullet lists in the story)."""
    client, deps, graph, _ = client_for(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    home = client.get("/").text
    file_input = home[home.index('<input id="upload-file"'):]
    file_input = file_input[:file_input.index(">")]
    assert 'style="position:absolute;width:1px;height:1px' in file_input and "opacity:0" in file_input
    page = client.get(f"/invoices/{state['run_id']}").text
    story = page[page.index("data-live-steps"):page.index('id="details-title"')]
    assert "<ol" not in story and "<ul" not in story and "1. Read the file" in story


def test_running_page_refreshes_without_javascript(conn):
    """A running invoice's page carries a noscript refresh, so it still advances with JavaScript off."""
    client, *_ = client_for(conn, [])
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    db.save_progress(conn, "r1", "read", {"run_id": "r1", "raw_text": "line", "format": "txt"})
    html = client.get("/invoices/r1").text
    assert '<noscript><meta http-equiv="refresh" content="3"></noscript>' in html

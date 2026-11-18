"""End-to-end tests for the top-level pipeline graph (read -> extract -> validate -> approval -> guardrail -> pay/reject/human review).
All LLM roles are scripted fakes and payment is a recording fake, so these run offline.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps, build_graph
from invoice_agent.infra import db
from invoice_agent.models import ExtractedField, ExtractedLineItem
from invoice_agent.utils.normalize import to_invoice
from invoice_agent.utils.validation.validate import fingerprint
from tests.fakes import (
    ScriptedAnalyst, ScriptedExtractor, ScriptedVP, make_extracted, make_recommendation, make_review,
)

INVOICES = Path(__file__).parent.parent / "data" / "invoices"
FULL_CHECKLIST = {k: "checked" for k in CHECKLIST_KEYS}


class RecordingPay:
    """Fake payment function that records every (vendor, amount) it is asked to pay."""

    def __init__(self):
        """Start with no recorded payments."""
        self.calls: list[tuple[str, Decimal]] = []

    def __call__(self, vendor: str, amount: Decimal) -> dict:
        """Record the payment and report success like mock_payment does."""
        self.calls.append((vendor, amount))
        return {"status": "success"}


def _f(value: str) -> ExtractedField:
    """High-confidence extracted field whose raw text equals its value."""
    return ExtractedField(value=value, raw=value, confidence="high")


def extracted_1002():
    """What a correct extraction of invoice_1002.txt looks like: 20 GadgetX against 5 in stock."""
    return make_extracted(
        invoice_number="1002", vendor="Gadgets Co.", date="2026-01-30", due_date="2026-01-30",
        subtotal=None, tax=None, total="15000.00", payment_terms="Net 30",
        items=[ExtractedLineItem(name=_f("GadgetX"), qty=_f("20"), unit_price=_f("750"))],
    )


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded app database for each test."""
    c = db.connect(tmp_path / "app.db")
    db.init_db(c)
    yield c
    c.close()


def make_graph(conn, extractor, analyst, vp):
    """Build the pipeline with fakes and an in-memory checkpointer; returns (graph, pay fake)."""
    pay = RecordingPay()
    deps = Deps(extractor=extractor, analyst=analyst, vp=vp, pay=pay, conn=conn)
    return build_graph(deps, InMemorySaver()), pay


def run(graph, name, run_id="run-1"):
    """Invoke the graph on a sample invoice file; returns (result, config) so tests can resume."""
    config = {"configurable": {"thread_id": run_id}}
    result = graph.invoke({"run_id": run_id, "source_path": str(INVOICES / name)}, config)
    return result, config


def send_backs(n=3):
    """n VP SEND_BACK reviews, enough to exhaust the approval rounds."""
    return [make_review(verdict="SEND_BACK", final_decision=None, feedback=f"fb {i}") for i in range(n)]


def test_clean_invoice_is_paid(conn):
    """1001 with approving agents is paid exactly once, to the right vendor and amount."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "PAID" and out["final"].decided_by == "agents"
    assert out["final"].payment_result == {"status": "success"}
    assert pay.calls == [("Widgets Inc.", Decimal("5000.00"))]
    assert len(ex.calls) == 1 and len(a.calls) == 1 and len(v.calls) == 1


def test_guardrail_blocks_agent_approval_over_must_reject(conn):
    """1002 (20 GadgetX, 5 in stock): agents approve, but the guardrail forces REJECT and nothing is paid."""
    ex = ScriptedExtractor([extracted_1002()])
    a = ScriptedAnalyst([make_recommendation(checklist=FULL_CHECKLIST)])
    v = ScriptedVP([make_review()])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1002.txt")
    final = out["final"]
    assert final.status == "REJECTED" and final.decided_by == "guardrail"
    assert final.reasoning.startswith("Blocked by guardrail: ")
    assert "GadgetX" in final.reasoning
    assert out["approval"].decision == "APPROVE"
    assert len(pay.calls) == 0
    assert len(ex.calls) == 1 and len(a.calls) == 1 and len(v.calls) == 1


def test_agent_reject_logs_reasoning(conn):
    """An accepted agent REJECT ends REJECTED by the agents, carrying the VP's reasoning; nothing is paid."""
    ex = ScriptedExtractor([make_extracted()])
    a = ScriptedAnalyst([make_recommendation("REJECT")])
    v = ScriptedVP([make_review(final_decision="REJECT", reasoning="price unexplained")])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "REJECTED" and out["final"].decided_by == "agents"
    assert out["final"].reasoning == "price unexplained"
    assert len(out["approval"].rounds) == 1
    assert len(pay.calls) == 0
    assert len(a.calls) == 1 and len(v.calls) == 1


def test_needs_human_review_pauses(conn):
    """Three VP send-backs pause the graph at human_review with the run id; nothing is paid yet."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert "__interrupt__" in out
    assert out["__interrupt__"][0].value == {"run_id": "run-1", "reason": "needs human review"}
    assert "final" not in out
    assert out["approval"].decision == "NEEDS_HUMAN_REVIEW"
    assert len(pay.calls) == 0
    assert len(a.calls) == 3 and len(v.calls) == 3


def test_human_approve_resumes_to_pay(conn):
    """Resuming a paused run with a human APPROVE pays it, recorded as the human's decision."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    _, config = run(graph, "invoice_1001.txt")
    out = graph.invoke(Command(resume={"decision": "APPROVE", "note": "ok", "justification": None}), config)
    assert out["final"].status == "PAID" and out["final"].decided_by == "human"
    assert out["final"].human_note == "ok"
    assert pay.calls == [("Widgets Inc.", Decimal("5000.00"))]
    # Resuming re-runs only human_review onward: no extra extractor or agent calls.
    assert len(ex.calls) == 1 and len(a.calls) == 3 and len(v.calls) == 3


def test_human_reject_logs_rejection(conn):
    """A human REJECT on resume ends REJECTED by the human, with their note, and nothing is paid."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    _, config = run(graph, "invoice_1001.txt")
    out = graph.invoke(Command(resume={"decision": "REJECT", "note": "vendor unknown"}), config)
    assert out["final"].status == "REJECTED" and out["final"].decided_by == "human"
    assert out["final"].human_note == "vendor unknown"
    assert len(pay.calls) == 0


def test_human_approve_over_must_reject_requires_justification(conn):
    """Approving 1002 over its must_reject flag fails without a justification, then succeeds with one."""
    ex = ScriptedExtractor([extracted_1002()])
    a = ScriptedAnalyst([make_recommendation(checklist=FULL_CHECKLIST)] * 3)
    v = ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    _, config = run(graph, "invoice_1002.txt")
    with pytest.raises(ValueError, match="justification required"):
        graph.invoke(Command(resume={"decision": "APPROVE", "note": "fine", "justification": "  "}), config)
    assert len(pay.calls) == 0
    # The run is still paused, so the reviewer can retry with a justification.
    out = graph.invoke(
        Command(resume={"decision": "APPROVE", "note": "fine", "justification": "backorder agreed with vendor"}),
        config,
    )
    assert out["final"].status == "PAID" and out["final"].decided_by == "human"
    assert out["final"].reasoning == "backorder agreed with vendor"
    assert pay.calls == [("Gadgets Co.", Decimal("15000.00"))]


def test_duplicate_short_circuits(conn):
    """An invoice already processed with identical contents ends DUPLICATE without calling the agents."""
    inv, _ = to_invoice(make_extracted(), db.all_skus(conn))
    db.save_run(conn, {"run_id": "prior", "content_hash": "other-file", "invoice": inv, "fingerprint": fingerprint(inv)})
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["duplicate_of"] == "prior"
    assert out["final"].status == "DUPLICATE" and out["final"].decided_by == "system"
    assert len(ex.calls) == 1 and len(a.calls) == 0 and len(v.calls) == 0 and len(pay.calls) == 0


def test_extractor_failure_ends_in_error(conn):
    """An extractor that fails on every attempt ends the run as ERROR (decided by the system) with the reason."""
    ex = ScriptedExtractor([RuntimeError("model timeout")] * 3)
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "model timeout" in out["final"].reasoning
    assert len(ex.calls) == 3 and len(a.calls) == 0 and len(pay.calls) == 0


def test_read_failure_ends_in_error(conn, tmp_path):
    """A file the reader cannot handle ends the run as ERROR before the extractor is ever called."""
    bad = tmp_path / "invoice.docx"
    bad.write_text("not supported")
    ex = ScriptedExtractor([])
    graph, pay = make_graph(conn, ex, ScriptedAnalyst([]), ScriptedVP([]))
    config = {"configurable": {"thread_id": "run-bad"}}
    out = graph.invoke({"run_id": "run-bad", "source_path": str(bad)}, config)
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "unsupported file extension" in out["final"].reasoning
    assert len(ex.calls) == 0 and len(pay.calls) == 0


def test_analyst_failure_ends_in_error(conn):
    """An analyst that raises (e.g. the Grok API is down) ends the run as ERROR instead of crashing; nothing is paid."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([RuntimeError("grok unavailable")]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "grok unavailable" in out["final"].reasoning
    assert len(a.calls) == 1 and len(v.calls) == 0 and len(pay.calls) == 0


def test_payment_failure_ends_in_error(conn):
    """A payment call that raises ends the run as ERROR (not PAID), with the payment error as the reason."""
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    calls = []

    def failing_pay(vendor, amount):
        """Record the attempt, then fail like a payment gateway outage."""
        calls.append((vendor, amount))
        raise ConnectionError("payment gateway down")

    graph = build_graph(Deps(extractor=ex, analyst=a, vp=v, pay=failing_pay, conn=conn), InMemorySaver())
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "payment gateway down" in out["final"].reasoning
    assert len(calls) == 1


def test_validation_failure_ends_in_error(conn, monkeypatch):
    """An unexpected crash inside validation ends the run as ERROR before the agents are called."""
    import invoice_agent.graph as graph_module

    def boom(*args, **kwargs):
        """Stand-in for a validation bug."""
        raise KeyError("items")

    monkeypatch.setattr(graph_module, "run_validation", boom)
    ex = ScriptedExtractor([make_extracted()])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "items" in out["final"].reasoning
    assert len(a.calls) == 0 and len(pay.calls) == 0

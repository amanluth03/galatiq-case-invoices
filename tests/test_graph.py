"""End-to-end tests for the top-level pipeline graph (read -> extract -> validate -> approval -> guardrail -> pay/reject/human review).
A broken hard rule (must_reject) is rejected in validate without the analyst/VP. All LLM roles are scripted fakes and payment is a recording fake, so these run offline.
"""
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps, build_graph, make_serializer
from invoice_agent.infra import db
from invoice_agent.models import ExtractedField, ExtractedLineItem
from invoice_agent.utils.normalize import to_invoice
from invoice_agent.utils.validation.validate import fingerprint
from tests.fakes import (
    ScriptedAnalyst, ScriptedIngestionAgent, ScriptedVP, make_extracted, make_recommendation, make_review,
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


def make_graph(conn, ingestion, analyst, vp):
    """Build the pipeline with fakes and an in-memory checkpointer; returns (graph, pay fake)."""
    pay = RecordingPay()
    deps = Deps(ingestion=ingestion, analyst=analyst, vp=vp, pay=pay, conn=conn)
    return build_graph(deps, InMemorySaver(serde=make_serializer())), pay


def run(graph, name, run_id="run-1"):
    """Invoke the graph on a sample invoice file; returns (result, config) so tests can resume."""
    config = {"configurable": {"thread_id": run_id}}
    result = graph.invoke({"run_id": run_id, "source_path": str(INVOICES / name)}, config)
    return result, config


def paused_before_shortcut(monkeypatch, graph, name):
    """Pause a must_reject invoice at human_review the way runs saved before the hard-rule shortcut could be.
    The shortcut is switched off only for the first run; resuming uses the real code."""
    import invoice_agent.graph as graph_module
    with monkeypatch.context() as m:
        m.setattr(graph_module, "apply_guardrail", lambda flags: None)
        out, config = run(graph, name)
    assert "__interrupt__" in out
    return config


def send_backs(n=3):
    """n VP SEND_BACK reviews, enough to exhaust the approval rounds."""
    return [make_review(verdict="SEND_BACK", final_decision=None, feedback=f"fb {i}") for i in range(n)]


def test_clean_invoice_is_paid(conn):
    """1001 with approving agents is paid exactly once, to the right vendor and amount."""
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "PAID" and out["final"].decided_by == "agents"
    assert out["final"].payment_result == {"status": "success"}
    assert pay.calls == [("Widgets Inc.", Decimal("5000.00"))]
    assert len(ex.calls) == 1 and len(a.calls) == 1 and len(v.calls) == 1


def test_hard_rule_rejects_without_the_agents(conn):
    """1002 (20 GadgetX, 5 in stock) is rejected by the rules straight after validate: no analyst/VP call, no payment."""
    ex = ScriptedIngestionAgent([extracted_1002()])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1002.txt")
    final = out["final"]
    assert final.status == "REJECTED" and final.decided_by == "guardrail"
    assert final.reasoning == "Rejected by the rules: Ordered 20 GadgetX, only 5 in stock."
    assert out.get("approval") is None
    assert len(pay.calls) == 0
    assert len(ex.calls) == 1 and len(a.calls) == 0 and len(v.calls) == 0


def test_agent_reject_logs_reasoning(conn):
    """An accepted agent REJECT ends REJECTED by the agents, carrying the VP's reasoning; nothing is paid."""
    ex = ScriptedIngestionAgent([make_extracted()])
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
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert "__interrupt__" in out
    assert out["__interrupt__"][0].value == {"run_id": "run-1", "reason": "needs human review"}
    assert out.get("final") is None
    assert out["approval"].decision == "NEEDS_HUMAN_REVIEW"
    assert len(pay.calls) == 0
    assert len(a.calls) == 3 and len(v.calls) == 3


def test_human_approve_resumes_to_pay(conn):
    """Resuming a paused run with a human APPROVE pays it, recorded as the human's decision."""
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    _, config = run(graph, "invoice_1001.txt")
    out = graph.invoke(Command(resume={"decision": "APPROVE", "note": "ok", "justification": None}), config)
    assert out["final"].status == "PAID" and out["final"].decided_by == "human"
    assert out["final"].human_note == "ok"
    assert pay.calls == [("Widgets Inc.", Decimal("5000.00"))]
    # Resuming re-runs only human_review onward: no extra ingestion agent or agent calls.
    assert len(ex.calls) == 1 and len(a.calls) == 3 and len(v.calls) == 3


def test_human_reject_logs_rejection(conn):
    """A human REJECT on resume ends REJECTED by the human, with their note, and nothing is paid."""
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    _, config = run(graph, "invoice_1001.txt")
    out = graph.invoke(Command(resume={"decision": "REJECT", "note": "vendor unknown"}), config)
    assert out["final"].status == "REJECTED" and out["final"].decided_by == "human"
    assert out["final"].human_note == "vendor unknown"
    assert len(pay.calls) == 0


def test_human_approve_over_must_reject_requires_justification(conn, monkeypatch):
    """A 1002 run paused before the shortcut existed: approving over its must_reject flag needs a justification."""
    ex = ScriptedIngestionAgent([extracted_1002()])
    a = ScriptedAnalyst([make_recommendation(checklist=FULL_CHECKLIST)] * 3)
    v = ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    config = paused_before_shortcut(monkeypatch, graph, "invoice_1002.txt")
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
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["duplicate_of"] == "prior"
    assert out["final"].status == "DUPLICATE" and out["final"].decided_by == "system"
    assert len(ex.calls) == 1 and len(a.calls) == 0 and len(v.calls) == 0 and len(pay.calls) == 0


def test_ingestion_agent_failure_ends_in_error(conn):
    """An ingestion agent that fails on every attempt ends the run as ERROR (decided by the system) with the reason."""
    ex = ScriptedIngestionAgent([RuntimeError("model timeout")] * 3)
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "model timeout" in out["final"].reasoning
    assert len(ex.calls) == 3 and len(a.calls) == 0 and len(pay.calls) == 0


def test_read_failure_ends_in_error(conn, tmp_path):
    """A file the reader cannot handle ends the run as ERROR before the ingestion agent is ever called."""
    bad = tmp_path / "invoice.docx"
    bad.write_text("not supported")
    ex = ScriptedIngestionAgent([])
    graph, pay = make_graph(conn, ex, ScriptedAnalyst([]), ScriptedVP([]))
    config = {"configurable": {"thread_id": "run-bad"}}
    out = graph.invoke({"run_id": "run-bad", "source_path": str(bad)}, config)
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "unsupported file extension" in out["final"].reasoning
    assert len(ex.calls) == 0 and len(pay.calls) == 0


def test_analyst_failure_ends_in_error(conn):
    """An analyst that raises (e.g. the Grok API is down) ends the run as ERROR instead of crashing; nothing is paid."""
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([RuntimeError("grok unavailable")]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "grok unavailable" in out["final"].reasoning
    assert len(a.calls) == 1 and len(v.calls) == 0 and len(pay.calls) == 0


def test_payment_failure_ends_in_error(conn):
    """A payment call that raises ends the run as ERROR (not PAID), with the payment error as the reason."""
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    calls = []

    def failing_pay(vendor, amount):
        """Record the attempt, then fail like a payment gateway outage."""
        calls.append((vendor, amount))
        raise ConnectionError("payment gateway down")

    graph = build_graph(Deps(ingestion=ex, analyst=a, vp=v, pay=failing_pay, conn=conn), InMemorySaver())
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
    ex = ScriptedIngestionAgent([make_extracted()])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1001.txt")
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert "items" in out["final"].reasoning
    assert len(a.calls) == 0 and len(pay.calls) == 0


def test_reused_thread_does_not_carry_over_previous_outcome(conn):
    """Re-running on the same thread_id starts clean: a paid first run cannot leak PAID into a rejected second run."""
    ex = ScriptedIngestionAgent([make_extracted(), make_extracted()])
    a = ScriptedAnalyst([make_recommendation(), make_recommendation("REJECT")])
    v = ScriptedVP([make_review(), make_review(final_decision="REJECT", reasoning="nope")])
    graph, pay = make_graph(conn, ex, a, v)
    first, _ = run(graph, "invoice_1001.txt", "same")
    second, _ = run(graph, "invoice_1001.txt", "same")
    assert first["final"].status == "PAID"
    assert second["approval"].decision == "REJECT"
    assert second["final"].status == "REJECTED" and second["final"].decided_by == "agents"
    assert second["final"].reasoning == "nope"
    assert len(pay.calls) == 1


def test_reused_thread_after_guardrail_block_uses_new_decision(conn):
    """A hard-rule rejection in run 1 must not be replayed as the rejection reason of a later agent REJECT on the same thread."""
    ex = ScriptedIngestionAgent([extracted_1002(), make_extracted()])
    a = ScriptedAnalyst([make_recommendation("REJECT")])
    v = ScriptedVP([make_review(final_decision="REJECT", reasoning="no PO on file")])
    graph, pay = make_graph(conn, ex, a, v)
    first, _ = run(graph, "invoice_1002.txt", "same")
    second, _ = run(graph, "invoice_1001.txt", "same")
    assert first["final"].decided_by == "guardrail"
    assert second["final"].decided_by == "agents" and second["final"].reasoning == "no PO on file"
    assert len(pay.calls) == 0


@pytest.mark.parametrize("field", ["vendor", "total"])
def test_pay_refuses_missing_vendor_or_total(conn, field, monkeypatch):
    """Even a justified human APPROVE cannot pay an invoice with no vendor or total: ERROR, and pay is never called."""
    ex = ScriptedIngestionAgent([make_extracted(**{field: ExtractedField(value=None, raw=None, confidence="low")})])
    a, v = ScriptedAnalyst([make_recommendation()] * 3), ScriptedVP(send_backs())
    graph, pay = make_graph(conn, ex, a, v)
    # Missing vendor/total is a hard rule, so only a run paused before the shortcut can reach a human here.
    config = paused_before_shortcut(monkeypatch, graph, "invoice_1001.txt")
    out = graph.invoke(Command(resume={"decision": "APPROVE", "note": "", "justification": "I know them"}), config)
    assert out["final"].status == "ERROR" and out["final"].decided_by == "system"
    assert out["final"].reasoning == "cannot pay: missing vendor/total"
    assert len(pay.calls) == 0


def test_sqlite_checkpoint_round_trip_has_no_unregistered_types(conn, tmp_path):
    """Pausing and resuming through SqliteSaver restores our models with no "unregistered type" warning or block."""
    import sqlite3

    from langgraph.checkpoint.serde.event_hooks import register_serde_event_listener
    from langgraph.checkpoint.sqlite import SqliteSaver

    from invoice_agent.models import FinalOutcome, HumanDecision

    events = []
    # Serde events fire on every load (the log warning fires only once per process), so this is order-independent.
    unregister = register_serde_event_listener(events.append)
    cp = sqlite3.connect(tmp_path / "checkpoints.db", check_same_thread=False)
    try:
        ex = ScriptedIngestionAgent([make_extracted()])
        a = ScriptedAnalyst([make_recommendation()] * 3)
        pay = RecordingPay()
        deps = Deps(ingestion=ex, analyst=a, vp=ScriptedVP(send_backs()), pay=pay, conn=conn)
        graph = build_graph(deps, SqliteSaver(cp, serde=make_serializer()))
        _, config = run(graph, "invoice_1001.txt")
        out = graph.invoke(Command(resume={"decision": "APPROVE", "note": "ok", "justification": None}), config)
        state = graph.get_state(config).values
    finally:
        unregister()
        cp.close()
    assert out["final"].status == "PAID" and len(pay.calls) == 1
    assert isinstance(state["final"], FinalOutcome) and isinstance(state["human_decision"], HumanDecision)
    assert [e for e in events if e["kind"] in ("msgpack_unregistered_allowed", "msgpack_blocked")] == []


def extracted_price_mismatch():
    """1001 billed at $260 per WidgetA instead of the $250 reference; the arithmetic itself is consistent."""
    return make_extracted(
        subtotal="5100.00", total="5100.00",
        items=[ExtractedLineItem(name=_f("WidgetA"), qty=_f("10"), unit_price=_f("260.00"), line_total=_f("2600.00")),
               ExtractedLineItem(name=_f("WidgetB"), qty=_f("5"), unit_price=_f("500.00"), line_total=_f("2500.00"))],
    )


def test_agent_approval_over_price_mismatch_goes_to_human(conn):
    """Agents approve an off-price invoice: the guardrail pauses it for a person instead of paying."""
    ex = ScriptedIngestionAgent([extracted_price_mismatch()])
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    graph, pay = make_graph(conn, ex, a, v)
    out, config = run(graph, "invoice_1001.txt")
    assert "price_mismatch" in {f.code for f in out["flags"]}
    assert "__interrupt__" in out and out.get("final") is None
    assert out["approval"].decision == "NEEDS_HUMAN_REVIEW"
    assert out["approval"].reasoning == (
        "Prices, totals or currency differ from Acme's records. The agents recommended APPROVE; a person decides.")
    assert len(pay.calls) == 0
    # The human's APPROVE is final: it pays, exactly once.
    done = graph.invoke(Command(resume={"decision": "APPROVE", "note": "rate agreed", "justification": None}), config)
    assert done["final"].status == "PAID" and done["final"].decided_by == "human"
    assert pay.calls == [("Widgets Inc.", Decimal("5100.00"))]
    assert len(a.calls) == 1 and len(v.calls) == 1


def test_agent_reject_over_price_mismatch_goes_to_human(conn):
    """Agents REJECT an off-price invoice with no hard-rule flag: a person decides, nothing is paid."""
    ex = ScriptedIngestionAgent([extracted_price_mismatch()])
    a = ScriptedAnalyst([make_recommendation("REJECT")])
    v = ScriptedVP([make_review(final_decision="REJECT", reasoning="price unexplained")])
    graph, pay = make_graph(conn, ex, a, v)
    out, config = run(graph, "invoice_1001.txt")
    assert "__interrupt__" in out and out.get("final") is None
    assert out["approval"].decision == "NEEDS_HUMAN_REVIEW"
    assert out["approval"].reasoning == (
        "Prices, totals or currency differ from Acme's records. The agents recommended REJECT; a person decides.")
    assert len(pay.calls) == 0


def test_must_reject_with_price_mismatch_is_rejected_without_agents_or_person(conn):
    """A hard-rule flag plus a price difference is rejected by the rules: no agents, no person step."""
    # 1002 again, but GadgetX billed at $800 instead of the $750 reference: exceeds_stock plus price_mismatch.
    ex = ScriptedIngestionAgent([make_extracted(
        invoice_number="1002", vendor="Gadgets Co.", date="2026-01-30", due_date="2026-01-30",
        subtotal=None, tax=None, total="16000.00", payment_terms="Net 30",
        items=[ExtractedLineItem(name=_f("GadgetX"), qty=_f("20"), unit_price=_f("800"))])])
    a, v = ScriptedAnalyst([]), ScriptedVP([])
    graph, pay = make_graph(conn, ex, a, v)
    out, _ = run(graph, "invoice_1002.txt")
    codes = {f.code for f in out["flags"]}
    assert {"exceeds_stock", "price_mismatch"} <= codes
    assert out["final"].status == "REJECTED" and out["final"].decided_by == "guardrail" and "__interrupt__" not in out
    assert len(pay.calls) == 0 and len(a.calls) == 0 and len(v.calls) == 0


def test_agent_approval_with_only_over_10k_and_info_flags_is_paid(conn, tmp_path):
    """An over-$10K invoice at reference prices with urgency wording is paid on the agents' APPROVE."""
    # The urgency flag comes from the raw text, so the file says it; the scripted extraction supplies the fields.
    source = tmp_path / "urgent.txt"
    # Amounts are printed too, so the amount-in-source check sees every extracted figure in the file.
    source.write_text("Widgets Inc. invoice: URGENT, please pay immediately\n"
                      "WidgetA 15 $250.00 $3,750.00\nWidgetB 10 $500.00 $5,000.00\nGadgetX 5 $750.00 $3,750.00\n"
                      "Subtotal $12,500.00\nTotal $12,500.00")
    ex = ScriptedIngestionAgent([make_extracted(
        subtotal="12500.00", total="12500.00",
        items=[ExtractedLineItem(name=_f("WidgetA"), qty=_f("15"), unit_price=_f("250.00"), line_total=_f("3750.00")),
               ExtractedLineItem(name=_f("WidgetB"), qty=_f("10"), unit_price=_f("500.00"), line_total=_f("5000.00")),
               ExtractedLineItem(name=_f("GadgetX"), qty=_f("5"), unit_price=_f("750.00"), line_total=_f("3750.00"))],
    )])
    a = ScriptedAnalyst([make_recommendation(checklist=FULL_CHECKLIST)])
    v = ScriptedVP([make_review()])
    graph, pay = make_graph(conn, ex, a, v)
    out = graph.invoke({"run_id": "run-1", "source_path": str(source)}, {"configurable": {"thread_id": "run-1"}})
    assert {(f.severity, f.code) for f in out["flags"]} == {("needs_scrutiny", "over_10k"), ("info", "urgency_language")}
    assert out["final"].status == "PAID" and out["final"].decided_by == "agents"
    assert pay.calls == [("Widgets Inc.", Decimal("12500.00"))]

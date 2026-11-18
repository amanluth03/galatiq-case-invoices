"""Tests for the runner: batch processing, dedupe, re-run policy and error containment, using fake agents and a tmp database."""
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from invoice_agent.graph import Deps, build_graph, make_serializer
from invoice_agent.infra import db
from invoice_agent.infra.logging_setup import setup_logging
from invoice_agent.runner import process_dir, process_file, resume_review
from tests.fakes import (
    ScriptedAnalyst, ScriptedIngestionAgent, ScriptedVP, make_extracted, make_recommendation, make_review,
)

INVOICES = Path(__file__).parent.parent / "data" / "invoices"


class RecordingPay:
    """Fake payment function that records every (vendor, amount) it is asked to pay."""

    def __init__(self):
        """Start with no recorded payments."""
        self.calls: list[tuple[str, Decimal]] = []

    def __call__(self, vendor: str, amount: Decimal) -> dict:
        """Record the payment and report success."""
        self.calls.append((vendor, amount))
        return {"status": "success"}


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded app database for each test."""
    c = db.connect(tmp_path / "app.db")
    db.init_db(c)
    yield c
    c.close()


def build(conn, extractions, n_approvals=10, send_back=False):
    """Fake deps and graph; extractions are scripted ingestion responses, agents approve (or always send back)."""
    pay = RecordingPay()
    ex = ScriptedIngestionAgent(extractions)
    if send_back:
        reviews = [make_review(verdict="SEND_BACK", final_decision=None, feedback="again")] * (3 * n_approvals)
    else:
        reviews = [make_review()] * n_approvals
    deps = Deps(ingestion=ex, analyst=ScriptedAnalyst([make_recommendation()] * (3 * n_approvals)),
                vp=ScriptedVP(reviews), pay=pay, conn=conn)
    return deps, build_graph(deps, InMemorySaver(serde=make_serializer())), pay


def copy_invoices(tmp_path, *names):
    """Copy sample invoices into a fresh directory so a batch has only the files a test wants."""
    d = tmp_path / "in"
    d.mkdir()
    for n in names:
        shutil.copy(INVOICES / n, d / n)
    return d


def statuses(states):
    """The final status of each state, in order."""
    return [s["final"].status for s in states]


def test_same_invoice_two_formats_in_batch(conn, tmp_path):
    """1011 as txt and as pdf extract to the same invoice: the first is PAID, the second DUPLICATE, paid once."""
    d = copy_invoices(tmp_path, "invoice_1011.txt", "invoice_1011.pdf")
    deps, graph, pay = build(conn, [make_extracted()] * 2)
    # Sorted by filename, so the .pdf is processed before the .txt; either way one pays and one is a duplicate.
    assert sorted(statuses(process_dir(d, deps, graph))) == ["DUPLICATE", "PAID"]
    assert len(pay.calls) == 1


def test_rerun_same_dir_does_not_self_duplicate(conn, tmp_path):
    """Running the same directory twice gives the same statuses and adds no run rows."""
    d = copy_invoices(tmp_path, "invoice_1011.txt", "invoice_1011.pdf")
    deps, graph, pay = build(conn, [make_extracted()] * 4)
    first = statuses(process_dir(d, deps, graph))
    count = len(db.list_runs(conn))
    assert statuses(process_dir(d, deps, graph)) == first
    assert len(db.list_runs(conn)) == count
    assert len(pay.calls) == 1


def test_revised_invoice_rejected(conn, tmp_path):
    """1004 then 1004_revised (same invoice number, new contents): the second is REJECTED with invoice_number_reused."""
    d = copy_invoices(tmp_path, "invoice_1004.json", "invoice_1004_revised.json")
    first = make_extracted(invoice_number="1004")
    second = make_extracted(invoice_number="1004", total="5100.00", subtotal="5100.00")
    deps, graph, _ = build(conn, [first, second])
    states = process_dir(d, deps, graph)
    assert statuses(states) == ["PAID", "REJECTED"]
    assert "invoice_number_reused" in [f.code for f in states[1]["flags"]]


def test_batch_continues_after_error(conn, tmp_path):
    """An extractor that keeps crashing (all 3 attempts) on one file ends that file as ERROR and the rest of the batch still runs."""
    d = copy_invoices(tmp_path, "invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt")
    deps, graph, _ = build(conn, [make_extracted(invoice_number="A"), *[RuntimeError("boom")] * 3,
                                  make_extracted(invoice_number="C")])
    assert statuses(process_dir(d, deps, graph)) == ["PAID", "ERROR", "PAID"]


def test_unexpected_exception_is_contained(conn, tmp_path):
    """A failure outside the graph's own handlers (here: the graph itself blowing up) still saves an ERROR run."""
    deps, _, _ = build(conn, [])

    class ExplodingGraph:
        """Graph stand-in whose stream always raises."""

        def stream(self, *a, **k):
            """Raise as an unexpected bug would."""
            raise RuntimeError("kaboom")

    state = process_file(INVOICES / "invoice_1001.txt", deps, ExplodingGraph())
    assert state["final"].status == "ERROR" and "kaboom" in state["final"].reasoning
    assert db.get_run(conn, state["run_id"])["status"] == "ERROR"


def test_rerun_of_paid_file_does_not_pay_again(conn):
    """Re-processing a PAID file returns the stored result and never calls pay or the agents again."""
    deps, graph, pay = build(conn, [make_extracted()])
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    again = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert again["final"].status == "PAID" and again["run_id"] == first["run_id"]
    assert len(pay.calls) == 1 and len(deps.ingestion.calls) == 1


def test_rerun_of_paused_review_keeps_it_paused(conn):
    """Re-processing a file awaiting human review leaves it paused (same run, agents not called again)."""
    deps, graph, _ = build(conn, [make_extracted()], send_back=True)
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert first["final"].status == "NEEDS_HUMAN_REVIEW" and first["final"].decided_by == "agents"
    again = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert again["final"].status == "NEEDS_HUMAN_REVIEW" and again["run_id"] == first["run_id"]
    assert len(deps.ingestion.calls) == 1


def test_rerun_of_error_replaces_result_under_same_run_id(conn):
    """A file that ended ERROR is re-run under the same run id and the new result replaces the old one."""
    deps, graph, _ = build(conn, [*[RuntimeError("boom")] * 3, make_extracted()])
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    second = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert first["final"].status == "ERROR" and second["final"].status == "PAID"
    assert second["run_id"] == first["run_id"] and len(db.list_runs(conn)) == 1


def test_paused_run_can_be_resumed(conn):
    """resume_review applies a human APPROVE to a paused run: it is paid and the saved run is updated."""
    deps, graph, pay = build(conn, [make_extracted()], send_back=True)
    paused = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    done = resume_review(paused["run_id"], "APPROVE", "ok", None, deps, graph)
    assert done["final"].status == "PAID" and done["final"].decided_by == "human"
    assert db.get_run(conn, paused["run_id"])["status"] == "PAID"
    assert len(pay.calls) == 1


def test_node_records_are_logged(conn, tmp_path):
    """Each graph node writes one node_completed record with run_id, node and duration to run.jsonl."""
    import json
    setup_logging(tmp_path / "logs")
    deps, graph, _ = build(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    records = [json.loads(l) for l in (tmp_path / "logs" / "run.jsonl").read_text().splitlines()]
    assert [r["node"] for r in records][:3] == ["read", "extract", "validate"]
    assert all(r["run_id"] == state["run_id"] and r["event"] == "node_completed" and "duration_ms" in r for r in records)

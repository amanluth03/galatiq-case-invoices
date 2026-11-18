"""Tests for the runner: batch processing (sequential and parallel), dedupe, re-run policy, end-of-batch retries and
error containment, using fake agents and a tmp database.
"""
import json
import logging
import shutil
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps, build_graph, make_serializer
from invoice_agent.infra import db
from invoice_agent.infra.logging_setup import setup_logging
from invoice_agent.runner import process_dir, process_file, resume_review
from tests.fakes import (
    _f, ScriptedAnalyst, ScriptedIngestionAgent, ScriptedVP, make_extracted, make_recommendation, make_review,
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


@pytest.fixture
def clean_logger():
    """Remove the handlers setup_logging adds to the invoice_agent logger once the test is over."""
    logger = logging.getLogger("invoice_agent")
    saved = (list(logger.handlers), logger.propagate, logger.level)
    yield
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for h in saved[0]:
        logger.addHandler(h)
    logger.propagate, logger.level = saved[1], saved[2]


def build(conn, extractions, n_approvals=10, send_back=False, checklist=None):
    """Fake deps and graph; extractions are scripted ingestion responses, agents approve (or always send back)."""
    pay = RecordingPay()
    ex = ScriptedIngestionAgent(extractions)
    if send_back:
        reviews = [make_review(verdict="SEND_BACK", final_decision=None, feedback="again")] * (3 * n_approvals)
    else:
        reviews = [make_review()] * n_approvals
    deps = Deps(ingestion=ex, analyst=ScriptedAnalyst([make_recommendation(checklist=checklist)] * (3 * n_approvals)),
                vp=ScriptedVP(reviews), pay=pay, conn=conn)
    return deps, build_graph(deps, InMemorySaver(serde=make_serializer())), pay


def build_factory(tmp_path, ingestion, n_approvals=40, checklist=None):
    """A runtime factory like the real one: each call opens its own connections to the same tmp app and checkpoint
    databases, while every runtime shares the given fake ingestion agent, approving agents and payment recorder."""
    pay = RecordingPay()
    analyst = ScriptedAnalyst([make_recommendation(checklist=checklist)] * (3 * n_approvals))
    vp = ScriptedVP([make_review()] * n_approvals)

    def factory():
        """A fresh runtime with its own SQLite connections, as each batch worker gets."""
        conn = db.connect(tmp_path / "app.db")
        db.init_db(conn)
        deps = Deps(ingestion=ingestion, analyst=analyst, vp=vp, pay=pay, conn=conn)
        checkpoints = sqlite3.connect(tmp_path / "checkpoints.db", check_same_thread=False)
        return deps, build_graph(deps, SqliteSaver(checkpoints, serde=make_serializer()))

    return factory, pay


class KeyedIngestion:
    """Thread-safe fake ingestion agent: answers by a marker found in the file text, so call order doesn't matter.
    Each marker maps to a list of responses used in turn (an Exception is raised); delays and a barrier force overlap."""

    def __init__(self, responses: dict, delays: dict | None = None, barrier: threading.Barrier | None = None):
        """Store the per-marker responses, optional per-marker sleep (seconds) and a barrier every call waits at."""
        self.responses = {k: list(v) for k, v in responses.items()}
        self.delays = delays or {}
        self.barrier = barrier
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def __call__(self, raw_text: str, errors: list[str]):
        """Find the marker in the text, wait/sleep as configured, then return (or raise) its next response."""
        marker = next(m for m in self.responses if m in raw_text)
        if self.barrier is not None and not errors:
            # Every run reaches extraction before any goes on to validate: the duplicate check really races.
            # Only first attempts wait; a self-correction retry (errors given) would wait alone.
            self.barrier.wait(timeout=10)
        time.sleep(self.delays.get(marker, 0))
        with self.lock:
            self.calls.append(marker)
            queue = self.responses[marker]
            response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response


def write_files(tmp_path, contents: dict[str, str]):
    """Write small invoice text files (name -> text) into a fresh directory and return it."""
    d = tmp_path / "in"
    d.mkdir()
    for name, text in contents.items():
        (d / name).write_text(text)
    return d


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
    factory, pay = build_factory(tmp_path, ScriptedIngestionAgent([make_extracted()] * 2))
    # Sorted by filename, so the .pdf is processed before the .txt; either way one pays and one is a duplicate.
    assert sorted(statuses(process_dir(d, factory).states)) == ["DUPLICATE", "PAID"]
    assert len(pay.calls) == 1


def test_rerun_same_dir_does_not_self_duplicate(conn, tmp_path):
    """Running the same directory twice gives the same statuses and adds no run rows."""
    d = copy_invoices(tmp_path, "invoice_1011.txt", "invoice_1011.pdf")
    factory, pay = build_factory(tmp_path, ScriptedIngestionAgent([make_extracted()] * 4))
    first = statuses(process_dir(d, factory).states)
    count = len(db.list_runs(conn))
    assert statuses(process_dir(d, factory).states) == first
    assert len(db.list_runs(conn)) == count
    assert len(pay.calls) == 1


def test_revised_invoice_rejected(conn, tmp_path):
    """1004 then 1004_revised (same invoice number, new contents): the second is REJECTED with invoice_number_reused."""
    d = copy_invoices(tmp_path, "invoice_1004.json", "invoice_1004_revised.json")
    first = make_extracted(invoice_number="1004")
    second = make_extracted(invoice_number="1004", total="5100.00", subtotal="5100.00")
    factory, _ = build_factory(tmp_path, ScriptedIngestionAgent([first, second]))
    states = process_dir(d, factory).states
    assert statuses(states) == ["PAID", "REJECTED"]
    assert "invoice_number_reused" in [f.code for f in states[1]["flags"]]


def test_batch_continues_after_error(conn, tmp_path):
    """An extractor that keeps crashing (all 3 attempts) on one file ends that file as ERROR and the rest of the batch still runs."""
    d = copy_invoices(tmp_path, "invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt")
    factory, _ = build_factory(tmp_path, ScriptedIngestionAgent(
        [make_extracted(invoice_number="A"), *[RuntimeError("boom")] * 3, make_extracted(invoice_number="C")]))
    # "boom" is not a timeout or connection error, so the end-of-batch retry leaves it alone.
    batch = process_dir(d, factory)
    assert statuses(batch.states) == ["PAID", "ERROR", "PAID"] and batch.retried == 0


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


def test_rerun_of_human_rejected_file_keeps_rejection(conn):
    """A run a human REJECTED is not re-run, so approving agents cannot overturn the human's decision."""
    deps, graph, pay = build(conn, [make_extracted()], send_back=True)
    paused = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    resume_review(paused["run_id"], "REJECT", "no", None, deps, graph)
    again = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert again["final"].status == "REJECTED" and again["final"].decided_by == "human"
    assert len(deps.ingestion.calls) == 1 and len(pay.calls) == 0


def test_crash_after_pay_is_not_paid_twice(conn, monkeypatch):
    """If saving fails right after pay succeeded, the re-run recovers the PAID checkpoint instead of paying again."""
    deps, graph, pay = build(conn, [make_extracted()])
    real_save, failed = db.save_run, []

    def flaky_save(c, state):
        """Fail the first save only, like a crash between pay and the final save."""
        if not failed:
            failed.append(1)
            raise RuntimeError("disk full")
        real_save(c, state)

    monkeypatch.setattr(db, "save_run", flaky_save)
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert first["final"].status == "ERROR" and len(pay.calls) == 1
    again = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert again["final"].status == "PAID" and again["run_id"] == first["run_id"]
    assert len(pay.calls) == 1 and db.get_run(conn, first["run_id"])["status"] == "PAID"


def test_rerun_of_rejected_has_no_stale_flags(conn):
    """A REJECTED run (with its flags) re-run with a clean extraction ends PAID and carries none of the old flags."""
    from tests.test_graph import extracted_1002
    deps, graph, _ = build(conn, [extracted_1002(), make_extracted()],
                          checklist={k: "checked" for k in CHECKLIST_KEYS})
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert first["final"].status == "REJECTED" and first["flags"]
    second = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    assert second["final"].status == "PAID" and second["run_id"] == first["run_id"]
    assert not any(f.severity == "must_reject" for f in second["flags"])
    assert not (db.get_run(conn, first["run_id"])["state"].get("guardrail_outcome"))


def test_node_records_are_logged(conn, tmp_path, clean_logger):
    """Each graph node writes one node_completed record with run_id, node and duration to run.jsonl."""
    import json
    setup_logging(tmp_path / "logs")
    deps, graph, _ = build(conn, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    records = [json.loads(l) for l in (tmp_path / "logs" / "run.jsonl").read_text().splitlines()]
    assert [r["node"] for r in records][:3] == ["read", "extract", "validate"]
    assert all(r["run_id"] == state["run_id"] and r["event"] == "node_completed" and "duration_ms" in r for r in records)


# ---------- parallel batches (Task 13) ----------

def _mixed_batch(tmp_path, workers):
    """Run six distinct invoices (paid, stock rejection, extraction error) with the given worker count."""
    from tests.test_graph import extracted_1002
    d = write_files(tmp_path, {f"f{i}.txt": f"marker-{i}" for i in range(6)})
    responses = {f"marker-{i}": [make_extracted(invoice_number=f"N{i}")] for i in (0, 2, 4)}
    responses["marker-1"] = [extracted_1002()]
    # A second over-stock invoice under its own number, so it isn't taken for a copy of the first.
    responses["marker-3"] = [extracted_1002().model_copy(update={"invoice_number": _f("N3")})]
    responses["marker-5"] = [RuntimeError("bad extraction")]
    # A full checklist lets the agents approve, so the over-stock invoices reach (and are stopped by) the guardrail.
    factory, pay = build_factory(tmp_path, KeyedIngestion(responses, delays={"marker-0": 0.2, "marker-1": 0.1}),
                                 checklist={k: "checked" for k in CHECKLIST_KEYS})
    return process_dir(d, factory, workers=workers), pay


def test_parallel_batch_matches_sequential(tmp_path):
    """Five workers give exactly the statuses one worker gives, results stay in filename order, and pay runs once per PAID."""
    (tmp_path / "seq").mkdir(), (tmp_path / "par").mkdir()
    seq, seq_pay = _mixed_batch(tmp_path / "seq", workers=1)
    par, par_pay = _mixed_batch(tmp_path / "par", workers=5)
    assert statuses(par.states) == statuses(seq.states) == ["PAID", "REJECTED", "PAID", "REJECTED", "PAID", "ERROR"]
    # f0 sleeps longest, so it finishes last in the parallel run; the output is still in filename order.
    assert [Path(s["source_path"]).name for s in par.states] == [f"f{i}.txt" for i in range(6)]
    assert len(par_pay.calls) == len(seq_pay.calls) == 3


def test_concurrent_same_content_pair_has_one_original(tmp_path):
    """Two copies of the same invoice validated at the same moment: exactly one is the original, the other DUPLICATE."""
    d = write_files(tmp_path, {"a.txt": "copy-a", "b.txt": "copy-b"})
    same = make_extracted()
    agent = KeyedIngestion({"copy-a": [same], "copy-b": [same]}, barrier=threading.Barrier(2))
    factory, pay = build_factory(tmp_path, agent)
    states = process_dir(d, factory, workers=5).states
    assert sorted(statuses(states)) == ["DUPLICATE", "PAID"]
    assert len(pay.calls) == 1


def test_concurrent_reused_number_pair_has_one_rejection(tmp_path):
    """Same invoice number, different contents, validated at the same moment: exactly one is rejected as reused."""
    d = write_files(tmp_path, {"a.txt": "copy-a", "b.txt": "copy-b"})
    first = make_extracted(invoice_number="1004")
    second = make_extracted(invoice_number="1004", total="5100.00", subtotal="5100.00")
    agent = KeyedIngestion({"copy-a": [first], "copy-b": [second]}, barrier=threading.Barrier(2))
    factory, _ = build_factory(tmp_path, agent)
    states = process_dir(d, factory, workers=5).states
    reused = [s for s in states if "invoice_number_reused" in [f.code for f in s["flags"]]]
    assert len(reused) == 1 and reused[0]["final"].status == "REJECTED"


def test_parallel_log_lines_stay_whole(tmp_path, clean_logger):
    """With five workers logging at once, every line of run.jsonl is still one complete JSON object."""
    setup_logging(tmp_path / "logs")
    d = write_files(tmp_path, {f"f{i}.txt": f"marker-{i}" for i in range(10)})
    agent = KeyedIngestion({f"marker-{i}": [make_extracted(invoice_number=f"N{i}")] for i in range(10)})
    factory, _ = build_factory(tmp_path, agent)
    states = process_dir(d, factory, workers=5).states
    records = [json.loads(line) for line in (tmp_path / "logs" / "run.jsonl").read_text().splitlines()]
    done = [r for r in records if r["event"] == "node_completed"]
    assert {r["run_id"] for r in done} == {s["run_id"] for s in states}


def test_timed_out_run_is_retried_once_at_end(tmp_path):
    """A run that ended ERROR on a Grok timeout is re-run once after the batch, under the same run id, and then paid."""
    d = write_files(tmp_path, {"a.txt": "marker-a", "b.txt": "marker-b"})
    timeout = RuntimeError("Request timed out.")
    agent = KeyedIngestion({"marker-a": [make_extracted(invoice_number="A")],
                            # Three failed attempts in the first pass (extraction tries 3 times), then a good answer.
                            "marker-b": [timeout, timeout, timeout, make_extracted(invoice_number="B")]})
    factory, pay = build_factory(tmp_path, agent)
    batch = process_dir(d, factory, workers=5)
    assert statuses(batch.states) == ["PAID", "PAID"] and batch.retried == 1
    rows = {Path(r["source_path"]).name: r for r in db.list_runs(db.connect(tmp_path / "app.db"))}
    assert len(rows) == 2 and rows["b.txt"]["run_id"] == batch.states[1]["run_id"]
    assert len(pay.calls) == 2


def test_timeout_that_persists_is_retried_only_once(tmp_path):
    """A run that still times out on its retry stays ERROR; the batch never retries it a second time."""
    d = write_files(tmp_path, {"a.txt": "marker-a"})
    agent = KeyedIngestion({"marker-a": [RuntimeError("Request timed out.")]})
    factory, _ = build_factory(tmp_path, agent)
    batch = process_dir(d, factory, workers=5)
    assert statuses(batch.states) == ["ERROR"] and batch.retried == 1
    # 3 extraction attempts in the first pass + 3 in the single retry.
    assert len(agent.calls) == 6

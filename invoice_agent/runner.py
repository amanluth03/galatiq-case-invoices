"""Runs the pipeline graph for files: one file, a whole directory, or resuming a paused human review.
Also builds the real runtime (Grok agents + SQLite checkpointer) and saves every run to the database.
Batches run several files at once (each worker thread on its own runtime), then retry Grok timeouts once.
The web screen calls process_file with on_stage and on_update to show each run's story live (including every
analyst/VP round inside the approval subgraph as it happens).
"""
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from invoice_agent.agents.analyst.agent import make_analyst
from invoice_agent.agents.approval.tools import make_tools
from invoice_agent.agents.ingestion.agent import make_ingestion_agent
from invoice_agent.agents.llm import make_model
from invoice_agent.agents.vp.agent import make_vp
from invoice_agent.graph import Deps, build_graph, make_serializer
from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
from invoice_agent.infra.logging_setup import RUN_LOGGER
from invoice_agent.models import (
    ApprovalOutcome, FinalOutcome, Flag, HumanDecision, Invoice, PipelineState, Uncertainty,
)
from invoice_agent.utils.payments import mock_payment
from invoice_agent.utils.reader import SUPPORTED_SUFFIXES, read_file

log = logging.getLogger(RUN_LOGGER)

# Runs in these states are never re-run: PAID must not pay twice, a paused review must not be discarded.
_KEEP_STATUSES = {"PAID", "NEEDS_HUMAN_REVIEW"}

# Error texts of a passing Grok/network problem (worth one more try), as the openai/xAI client words them:
# "Request timed out.", "Connection error.", "Error code: 502 - ...". Bad data or a 429 rate limit isn't retried.
_TRANSIENT_ERROR = re.compile(r"timed out|timeout|connection error|error code: 5\d\d", re.IGNORECASE)

# A function that builds a fresh runtime (deps + graph) with its own database connections.
RuntimeFactory = Callable[[], tuple[Deps, object]]
Job = TypeVar("Job")


def build_runtime(settings: Settings) -> tuple[Deps, object]:
    """Wire the real Grok agents, the app database and a SQLite checkpointer; raises MissingApiKey without a key."""
    # make_model first: with no key we fail before creating any files.
    model = make_model(settings)
    conn = db.connect(settings.app_db)
    db.init_db(conn)
    tools = make_tools(conn)
    deps = Deps(ingestion=make_ingestion_agent(model), analyst=make_analyst(model, tools),
                vp=make_vp(model, tools), pay=mock_payment, conn=conn)
    # check_same_thread=False: the web server resumes runs from worker threads.
    cp_conn = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
    # The serde must be ours, or paused runs cannot restore our pydantic models from the checkpoint.
    graph = build_graph(deps, SqliteSaver(cp_conn, serde=make_serializer()))
    return deps, graph


def hydrate(state: dict) -> PipelineState:
    """Turn a stored state_json dict back into typed models so callers see the same shape as a fresh run."""
    out = dict(state)
    single = {"invoice": Invoice, "approval": ApprovalOutcome, "human_decision": HumanDecision,
              "guardrail_outcome": FinalOutcome, "final": FinalOutcome}
    for key, cls in single.items():
        if out.get(key):
            out[key] = cls.model_validate(out[key])
    for key, cls in (("flags", Flag), ("uncertainties", Uncertainty)):
        out[key] = [cls.model_validate(x) for x in out.get(key, [])]
    return out  # type: ignore[return-value]


def keeps_result(existing: dict) -> bool:
    """True when a prior run of the same file must be returned as-is instead of re-run (paid, paused or human-decided)."""
    # A human's decision (even a rejection) is never overturned by re-running the agents.
    decided_by = (existing["state"].get("final") or {}).get("decided_by")
    return existing["status"] in _KEEP_STATUSES or decided_by == "human"


def _summary(update) -> dict:
    """A short log summary of one node's output: final status, flag codes or error."""
    if not isinstance(update, dict):
        return {}
    summary: dict = {}
    if update.get("final"):
        summary["status"] = update["final"].status
    if update.get("flags"):
        summary["flags"] = [f.code for f in update["flags"]]
    if update.get("error"):
        summary["error"] = update["error"]
    return summary


def _error_state(run_id: str, path: Path, exc: Exception, content_hash: str | None) -> PipelineState:
    """A saved-able ERROR state for a file that crashed outside the graph's own handlers."""
    # content_hash lets a re-run find this row and reuse the run id (and its checkpoint) instead of starting anew.
    state: PipelineState = {"run_id": run_id, "source_path": str(path),
            "final": FinalOutcome(status="ERROR", decided_by="system", reasoning=str(exc) or type(exc).__name__)}
    if content_hash:
        state["content_hash"] = content_hash
    return state


def _paid_checkpoint(graph, run_id: str) -> PipelineState | None:
    """The checkpointed state of a thread if it already ended PAID, else None."""
    try:
        state = dict(graph.get_state({"configurable": {"thread_id": run_id}}).values)
    except Exception:
        return None
    final = state.get("final")
    return state if final is not None and final.status == "PAID" else None  # type: ignore[return-value]


def _finish(graph, config: dict, run_id: str, paused: bool, deps: Deps) -> PipelineState:
    """Read the finished (or paused) state from the graph, set the paused outcome if needed, and save the run."""
    state = dict(graph.get_state(config).values)
    if paused:
        # No final outcome exists yet: the run waits for a human, with the agents' reasoning as context.
        state["final"] = FinalOutcome(status="NEEDS_HUMAN_REVIEW", decided_by="agents",
                                      reasoning=state["approval"].reasoning)
    state["run_id"] = run_id
    db.save_run(deps.conn, state)
    return state  # type: ignore[return-value]


def _report_stage(on_stage: Callable[[str], None] | None, node: str, run_id: str) -> None:
    """Tell the caller a node finished; a failing progress update is logged but never fails the run."""
    if on_stage is None:
        return
    try:
        on_stage(node)
    except Exception as exc:
        log.warning("stage update failed", extra={"fields": {"run_id": run_id, "node": node, "error": str(exc)}})


def _report_update(on_update: Callable[[tuple, str, dict], None] | None, namespace: tuple, node: str, update,
                   run_id: str) -> None:
    """Hand one node's output (top level or inside a subgraph) to the caller; a failing callback never fails the run."""
    if on_update is None or not isinstance(update, dict):
        return
    try:
        on_update(namespace, node, update)
    except Exception as exc:
        log.warning("progress update failed", extra={"fields": {"run_id": run_id, "node": node, "error": str(exc)}})


def process_file(path: Path, deps: Deps, graph, run_id: str | None = None,
                 on_stage: Callable[[str], None] | None = None,
                 on_update: Callable[[tuple, str, dict], None] | None = None) -> PipelineState:
    """Run one invoice file through the graph and save the result; never raises for a bad file.
    run_id: use this id (the web screen creates the run row first); on_stage: called with each top-level node name
    as it finishes; on_update: called with (namespace, node, update) for every node, () being the top level and
    ("approval:<id>", ...) the analyst/VP loop, so the screen can show each round as it arrives."""
    path = Path(path)
    run_id = run_id or uuid.uuid4().hex[:12]
    content_hash = None
    try:
        # Re-processing policy: look the file up by content, so the same bytes keep the same run id.
        content_hash = read_file(path).content_hash
        existing = db.find_run_by_content_hash(deps.conn, content_hash)
        if existing:
            if keeps_result(existing):
                return hydrate(existing["state"])
            # A crash after pay but before the final save leaves ERROR in the DB while the checkpoint says PAID:
            # recover that result instead of re-running, so the vendor is never paid twice.
            saved = _paid_checkpoint(graph, existing["run_id"])
            if saved is not None:
                db.save_run(deps.conn, saved)
                return saved
            # REJECTED / ERROR / DUPLICATE: re-run under the same id so the new result replaces the old row.
            run_id = existing["run_id"]
    except Exception:
        # Unreadable file: let the graph's read node report it as an ERROR run.
        pass
    config = {"configurable": {"thread_id": run_id}}
    paused = False
    try:
        last = time.perf_counter()
        # Streaming updates (not invoke) gives one chunk per node, which is what the run log records.
        # subgraphs=True also streams the nodes inside the approval loop; they reach on_update only, never the log.
        stream = graph.stream({"run_id": run_id, "source_path": str(path)}, config, stream_mode="updates",
                              subgraphs=True)
        for namespace, chunk in stream:
            for node, update in chunk.items():
                if namespace:
                    _report_update(on_update, namespace, node, update, run_id)
                    continue
                now = time.perf_counter()
                if node == "__interrupt__":
                    paused = True
                    continue
                log.info("node_completed", extra={"fields": {
                    "run_id": run_id, "node": node, "duration_ms": round((now - last) * 1000), **_summary(update)}})
                last = now
                _report_stage(on_stage, node, run_id)
                _report_update(on_update, namespace, node, update, run_id)
        return _finish(graph, config, run_id, paused, deps)
    except Exception as exc:
        log.warning("run failed", extra={"fields": {"run_id": run_id, "error": str(exc)}})
        state = _error_state(run_id, path, exc, content_hash)
        db.save_run(deps.conn, state)
        return state


def invoice_files(dir: Path) -> list[Path]:
    """Every supported invoice file in a directory, in filename order."""
    # Sorted only so output comes in a stable filename order; duplicate detection doesn't depend on it (workers run in parallel).
    return sorted(p for p in Path(dir).iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)


@dataclass
class BatchResult:
    """A finished batch: one final state per input, in input order, and how many timed-out runs were retried."""
    states: list[PipelineState]
    retried: int = 0


def close_runtime(deps: Deps, graph) -> None:
    """Close a runtime's own database connections (run history and checkpoints)."""
    deps.conn.close()
    checkpoint_conn = getattr(getattr(graph, "checkpointer", None), "conn", None)
    if isinstance(checkpoint_conn, sqlite3.Connection):
        checkpoint_conn.close()


def is_transient_error(state: PipelineState) -> bool:
    """True when a run ended ERROR because Grok or the network didn't answer (timeout, connection drop, 5xx)."""
    final = state.get("final")
    return final is not None and final.status == "ERROR" and bool(_TRANSIENT_ERROR.search(final.reasoning))


def run_batch(jobs: list[Job], work: Callable[[Job, Deps, object], PipelineState],
              runtime_factory: RuntimeFactory, workers: int) -> BatchResult:
    """Run work(job, deps, graph) for every job on up to `workers` threads, then re-run transient ERRORs once.
    Each thread builds its own runtime: a SQLite connection is never shared between threads."""
    local = threading.local()
    built: list[tuple[Deps, object]] = []
    built_lock = threading.Lock()

    def runtime() -> tuple[Deps, object]:
        """This thread's runtime, built on first use."""
        if getattr(local, "runtime", None) is None:
            local.runtime = runtime_factory()
            with built_lock:
                built.append(local.runtime)
        return local.runtime

    def one(job: Job) -> PipelineState:
        """Process one job on this thread's runtime."""
        deps, graph = runtime()
        return work(job, deps, graph)

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # map keeps input order, whatever order the runs finish in.
            states = list(pool.map(one, jobs))
        # A Grok slowdown can time out a few runs mid-batch; one more try, one at a time, usually gets through.
        # The normal re-run rules apply (same run id; PAID/paused/human-decided runs are never re-run).
        retry = [i for i, s in enumerate(states) if is_transient_error(s)]
        for i in retry:
            log.warning("retrying timed-out run", extra={"fields": {"run_id": states[i]["run_id"]}})
            states[i] = one(jobs[i])
        return BatchResult(states=states, retried=len(retry))
    finally:
        for deps, graph in built:
            close_runtime(deps, graph)


def process_dir(dir: Path, runtime_factory: RuntimeFactory, workers: int = 1) -> BatchResult:
    """Process every supported file in a directory, `workers` at a time; results come back in filename order."""
    return run_batch(invoice_files(dir), lambda path, deps, graph: process_file(path, deps, graph),
                     runtime_factory, workers)


def resume_review(run_id: str, decision: str, note: str, justification: str | None,
                  deps: Deps, graph) -> PipelineState:
    """Apply a human decision to a paused run, finish the graph and save the result."""
    config = {"configurable": {"thread_id": run_id}}
    graph.invoke(Command(resume={"decision": decision, "note": note, "justification": justification}), config)
    return _finish(graph, config, run_id, False, deps)

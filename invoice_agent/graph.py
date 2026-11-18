"""The top-level pipeline graph: read -> extract -> validate -> approval -> guardrail -> pay / log_rejection / human_review.
Code decides every step; the LLM roles (extractor, analyst, VP) and payment are injected through Deps so tests can use fakes.
"""
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from invoice_agent.agents.approval.loop import AnalystFn, ApprovalContext, VPFn, build_approval_graph
from invoice_agent.agents.approval.policy import APPROVAL_POLICY
from invoice_agent.agents.extractor.agent import Extractor, run_extraction
from invoice_agent.infra.db import all_skus
from invoice_agent.models import FinalOutcome, HumanDecision, JustifiedHumanDecision, PipelineState
from invoice_agent.utils.guardrail import apply_guardrail
from invoice_agent.utils.reader import read_file
from invoice_agent.utils.validation.validate import find_duplicate, fingerprint, run_validation


@dataclass
class Deps:
    """Everything the graph calls out to: the three LLM roles, the payment function and the app database."""
    extractor: Extractor
    analyst: AnalystFn
    vp: VPFn
    pay: Callable[[str, Decimal], dict]
    conn: sqlite3.Connection


def _error_text(exc: Exception) -> str:
    """The exception message, or its type name when the message is empty (e.g. a bare KeyError)."""
    return str(exc) or type(exc).__name__


def _has_must_reject(state: PipelineState) -> bool:
    """True when validation raised at least one must_reject flag."""
    return any(f.severity == "must_reject" for f in state.get("flags", []))


def build_graph(deps: Deps, checkpointer) -> CompiledStateGraph:
    """Wire all pipeline stages into one graph; the checkpointer lets human_review pause and resume."""
    approval_graph = build_approval_graph(deps.analyst, deps.vp)

    def read(state: PipelineState) -> Command[Literal["extract", "fail"]]:
        """Read the file into raw text; any failure (bad extension, corrupt PDF, bad encoding) goes to fail."""
        try:
            r = read_file(Path(state["source_path"]))
        except Exception as exc:
            return Command(goto="fail", update={"error": _error_text(exc)})
        # Reader issues (e.g. scanned PDF) travel with extraction issues so validation flags them.
        return Command(goto="extract", update={
            "raw_text": r.raw_text, "format": r.format, "source_path": r.source_path,
            "content_hash": r.content_hash, "extraction_issues": r.issues,
        })

    def extract(state: PipelineState) -> Command[Literal["validate", "fail"]]:
        """Ask the extractor for a typed invoice (with self-correction); a total failure goes to fail."""
        try:
            res = run_extraction(state["raw_text"], deps.extractor, all_skus(deps.conn))
        except Exception as exc:
            # Covers ExtractionFailed, a missing API key, network errors: no invoice means nothing to validate.
            return Command(goto="fail", update={"error": _error_text(exc)})
        return Command(goto="validate", update={
            "invoice": res.invoice, "uncertainties": res.uncertainties,
            "extraction_issues": [*state.get("extraction_issues", []), *res.issues],
        })

    def validate(state: PipelineState) -> dict:
        """Run the rulebook and the duplicate check, merging both sets of flags."""
        inv = state["invoice"]
        flags = run_validation(deps.conn, inv, state["raw_text"], state.get("extraction_issues", []))
        fp = fingerprint(inv)
        dup = find_duplicate(deps.conn, inv, fp, state["content_hash"], state["run_id"])
        return {"flags": [*flags, *dup.flags], "fingerprint": fp, "duplicate_of": dup.duplicate_of}

    def route_after_validate(state: PipelineState) -> Literal["mark_duplicate", "approval"]:
        """An exact duplicate stops here; everything else goes to the agents."""
        return "mark_duplicate" if state.get("duplicate_of") else "approval"

    def mark_duplicate(state: PipelineState) -> dict:
        """End the run as DUPLICATE, pointing at the original run."""
        return {"final": FinalOutcome(
            status="DUPLICATE", decided_by="system",
            reasoning=f"Duplicate of run {state['duplicate_of']}: same invoice number and contents",
        )}

    def approval(state: PipelineState) -> dict:
        """Run the analyst <-> VP critique loop on this invoice and store its outcome."""
        ctx = ApprovalContext(
            invoice=state["invoice"], flags=state.get("flags", []),
            uncertainties=state.get("uncertainties", []), policy=APPROVAL_POLICY,
        )
        out = approval_graph.invoke({"context": ctx, "rounds": [], "outcome": None})
        return {"approval": out["outcome"]}

    def route_after_approval(state: PipelineState) -> Literal["guardrail", "log_rejection", "human_review"]:
        """APPROVE must still pass the guardrail; REJECT is logged; an unsettled loop goes to a human."""
        return {
            "APPROVE": "guardrail", "REJECT": "log_rejection", "NEEDS_HUMAN_REVIEW": "human_review",
        }[state["approval"].decision]

    def guardrail(state: PipelineState) -> Command[Literal["pay", "log_rejection"]]:
        """Block an agent APPROVE when any must_reject flag is set; otherwise let it through to pay."""
        forced = apply_guardrail(state.get("flags", []))
        if forced is None:
            return Command(goto="pay")
        return Command(goto="log_rejection", update={"final": forced})

    def human_review(state: PipelineState) -> Command[Literal["pay", "log_rejection"]]:
        """Pause for a human decision; on resume, a human APPROVE goes straight to pay (it overrides the guardrail)."""
        # With must_reject flags the resume schema itself demands a justification for APPROVE. Checking inside
        # the schema matters: LangGraph saves a resume answer only once it validates, so a rejected answer
        # leaves the run paused and the reviewer can resume again (a check after interrupt() would save it).
        schema = JustifiedHumanDecision if _has_must_reject(state) else HumanDecision
        try:
            # First run: interrupt() pauses the graph here. On resume it returns the validated decision.
            decision = interrupt({"run_id": state["run_id"], "reason": "needs human review"}, response_schema=schema)
        except ValidationError as exc:
            if any("justification required" in e["msg"] for e in exc.errors()):
                # Surface the plain message callers (CLI, review screen) check for.
                raise ValueError("justification required") from None
            raise
        goto = "pay" if decision.decision == "APPROVE" else "log_rejection"
        return Command(goto=goto, update={"human_decision": decision})

    def pay(state: PipelineState) -> dict:
        """Pay the vendor the invoice total and record who approved it."""
        inv = state["invoice"]
        result = deps.pay(inv.vendor, inv.total)
        human = state.get("human_decision")
        if human:
            reasoning = (human.justification or "").strip() or human.note or "Approved by human reviewer"
            final = FinalOutcome(status="PAID", decided_by="human", reasoning=reasoning,
                                 payment_result=result, human_note=human.note)
        else:
            final = FinalOutcome(status="PAID", decided_by="agents", reasoning=state["approval"].reasoning,
                                 payment_result=result)
        return {"final": final}

    def log_rejection(state: PipelineState) -> dict:
        """Record the rejection and who made it (guardrail, human or agents); the runner persists the full state."""
        if state.get("final"):
            # The guardrail already wrote its forced rejection.
            return {}
        human = state.get("human_decision")
        if human:
            final = FinalOutcome(status="REJECTED", decided_by="human",
                                 reasoning=human.note or "Rejected by human reviewer", human_note=human.note)
        else:
            final = FinalOutcome(status="REJECTED", decided_by="agents", reasoning=state["approval"].reasoning)
        return {"final": final}

    def fail(state: PipelineState) -> dict:
        """End the run as ERROR with the reason from read or extract."""
        return {"final": FinalOutcome(status="ERROR", decided_by="system", reasoning=state["error"])}

    g = StateGraph(PipelineState)
    for node in (read, extract, validate, mark_duplicate, approval, guardrail, human_review, pay, log_rejection, fail):
        g.add_node(node.__name__, node)
    g.add_edge(START, "read")
    # read, extract, guardrail and human_review route themselves with Command(goto=...).
    g.add_conditional_edges("validate", route_after_validate)
    g.add_conditional_edges("approval", route_after_approval)
    for terminal in ("mark_duplicate", "pay", "log_rejection", "fail"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer)

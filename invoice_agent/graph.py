"""The top-level pipeline graph: read -> extract -> validate -> approval -> guardrail -> pay / log_rejection / human_review.
Code decides every step; the LLM roles (ingestion agent, analyst, VP) and payment are injected through Deps so tests can use fakes.
"""
import inspect
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError

from invoice_agent import models

from invoice_agent.agents.approval.loop import AnalystFn, ApprovalContext, VPFn, build_approval_graph
from invoice_agent.agents.approval.policy import APPROVAL_POLICY
from invoice_agent.agents.ingestion.agent import IngestionAgent, run_extraction
from invoice_agent.infra.db import all_skus
from invoice_agent.models import FinalOutcome, HumanDecision, JustifiedHumanDecision, PipelineState
from invoice_agent.utils.guardrail import apply_guardrail, needs_human_confirmation
from invoice_agent.utils.reader import read_file
from invoice_agent.utils.validation.validate import claim_identity, fingerprint, run_validation


@dataclass
class Deps:
    """Everything the graph calls out to: the three LLM roles, the payment function and the app database."""
    ingestion: IngestionAgent
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


def make_serializer() -> JsonPlusSerializer:
    """Checkpoint serializer that explicitly allows our own pydantic types (every checkpointer for build_graph uses it)."""
    # LangGraph warns on (and will soon block) restoring unregistered classes from a checkpoint,
    # so list every model in invoice_agent.models plus the approval loop's ApprovalContext.
    ours = [cls for _, cls in inspect.getmembers(models, inspect.isclass)
            if issubclass(cls, BaseModel) and cls.__module__ == models.__name__]
    return JsonPlusSerializer(allowed_msgpack_modules=[*ours, ApprovalContext])


# Added to a rule-based rejection when the analyst/VP call failed: the outcome stands without their write-up.
AGENTS_UNAVAILABLE_NOTE = ("The agents' write-up was unavailable (Grok didn't respond), "
                           "but these rules block payment on their own.")


# Keys a run produces. read clears them so a re-run on the same thread_id never sees the last run's results.
_RUN_RESET: dict = {
    "invoice": None, "fingerprint": None, "duplicate_of": None, "flags": [], "uncertainties": [],
    "extraction_issues": [], "approval": None, "human_decision": None, "guardrail_outcome": None,
    "final": None, "error": None,
}


def build_graph(deps: Deps, checkpointer) -> CompiledStateGraph:
    """Wire all pipeline stages into one graph; the checkpointer lets human_review pause and resume."""
    approval_graph = build_approval_graph(deps.analyst, deps.vp)

    def read(state: PipelineState) -> Command[Literal["extract", "fail"]]:
        """Read the file into raw text; any failure (bad extension, corrupt PDF, bad encoding) goes to fail."""
        try:
            r = read_file(Path(state["source_path"]))
        except Exception as exc:
            return Command(goto="fail", update={**_RUN_RESET, "error": _error_text(exc)})
        # Reader issues (e.g. scanned PDF) travel with extraction issues so validation flags them.
        return Command(goto="extract", update={
            **_RUN_RESET, "raw_text": r.raw_text, "format": r.format, "source_path": r.source_path,
            "content_hash": r.content_hash, "extraction_issues": r.issues,
        })

    def extract(state: PipelineState) -> Command[Literal["validate", "fail"]]:
        """Ask the ingestion agent for a typed invoice (with self-correction); a total failure goes to fail."""
        try:
            res = run_extraction(state["raw_text"], deps.ingestion, all_skus(deps.conn))
        except Exception as exc:
            # Covers ExtractionFailed, a missing API key, network errors: no invoice means nothing to validate.
            return Command(goto="fail", update={"error": _error_text(exc)})
        return Command(goto="validate", update={
            "invoice": res.invoice, "uncertainties": res.uncertainties,
            "extraction_issues": [*state.get("extraction_issues", []), *res.issues],
        })

    def validate(state: PipelineState) -> Command[Literal["mark_duplicate", "approval", "fail"]]:
        """Run the rulebook and the duplicate check; an exact duplicate stops here, everything else goes to the agents."""
        inv = state["invoice"]
        try:
            flags = run_validation(deps.conn, inv, state["raw_text"], state.get("extraction_issues", []))
            fp = fingerprint(inv)
            dup = claim_identity(deps.conn, inv, fp, state["content_hash"], state["run_id"], state["source_path"])
        except Exception as exc:
            # A bug or DB error here must end the run as ERROR, never crash the caller.
            return Command(goto="fail", update={"error": _error_text(exc)})
        goto = "mark_duplicate" if dup.duplicate_of else "approval"
        return Command(goto=goto, update={
            "flags": [*flags, *dup.flags], "fingerprint": fp, "duplicate_of": dup.duplicate_of,
        })

    def mark_duplicate(state: PipelineState) -> dict:
        """End the run as DUPLICATE, pointing at the original run."""
        return {"final": FinalOutcome(
            status="DUPLICATE", decided_by="system",
            reasoning=f"Duplicate of run {state['duplicate_of']}: same invoice number and contents",
        )}

    def approval(state: PipelineState) -> Command[Literal["guardrail", "log_rejection", "human_review", "fail"]]:
        """Run the analyst <-> VP loop; APPROVE must still pass the guardrail, REJECT is logged, unsettled goes to a human.
        If the agents fail, a must_reject flag still rejects the invoice; otherwise the run ends as ERROR."""
        ctx = ApprovalContext(
            invoice=state["invoice"], flags=state.get("flags", []),
            uncertainties=state.get("uncertainties", []), policy=APPROVAL_POLICY,
        )
        try:
            out = approval_graph.invoke({"context": ctx, "rounds": [], "outcome": None})
        except Exception as exc:
            forced = apply_guardrail(state.get("flags", []))
            if forced is not None:
                # Fail closed: a must_reject flag already decides the outcome, so a Grok timeout here still rejects.
                forced = forced.model_copy(update={"reasoning": f"{forced.reasoning}. {AGENTS_UNAVAILABLE_NOTE}"})
                return Command(goto="log_rejection", update={"guardrail_outcome": forced})
            # E.g. Grok timeout or malformed agent output on a clean invoice: no decision means ERROR, not a crash.
            return Command(goto="fail", update={"error": _error_text(exc)})
        outcome = out["outcome"]
        goto = {"APPROVE": "guardrail", "REJECT": "log_rejection", "NEEDS_HUMAN_REVIEW": "human_review"}[outcome.decision]
        return Command(goto=goto, update={"approval": outcome})

    def guardrail(state: PipelineState) -> Command[Literal["pay", "log_rejection", "human_review"]]:
        """Block an agent APPROVE over any must_reject flag; send price/total deviations to a human; otherwise pay."""
        flags = state.get("flags", [])
        forced = apply_guardrail(flags)
        if forced is not None:
            # must_reject wins over everything, including a needed human confirmation.
            return Command(goto="log_rejection", update={"guardrail_outcome": forced})
        reason = needs_human_confirmation(flags)
        if reason is not None:
            # The rounds keep the agents' own reasoning; the outcome now says why a person must look.
            held = state["approval"].model_copy(update={"decision": "NEEDS_HUMAN_REVIEW", "reasoning": reason})
            return Command(goto="human_review", update={"approval": held})
        return Command(goto="pay")

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

    def pay(state: PipelineState) -> Command[Literal["__end__", "fail"]]:
        """Pay the vendor the invoice total and record who approved it; a payment error ends the run as ERROR."""
        inv = state["invoice"]
        if not (inv.vendor or "").strip() or inv.total is None:
            # Last line of defence (e.g. a human approved over missing_vendor): never pay an unknown payee or amount.
            return Command(goto="fail", update={"error": "cannot pay: missing vendor/total"})
        try:
            result = deps.pay(inv.vendor, inv.total)
        except Exception as exc:
            return Command(goto="fail", update={"error": _error_text(exc)})
        human = state.get("human_decision")
        if human:
            reasoning = (human.justification or "").strip() or human.note or "Approved by human reviewer"
            final = FinalOutcome(status="PAID", decided_by="human", reasoning=reasoning,
                                 payment_result=result, human_note=human.note)
        else:
            final = FinalOutcome(status="PAID", decided_by="agents", reasoning=state["approval"].reasoning,
                                 payment_result=result)
        return Command(goto=END, update={"final": final})

    def log_rejection(state: PipelineState) -> dict:
        """Record the rejection and who made it (guardrail, human or agents); the runner persists the full state."""
        forced = state.get("guardrail_outcome")
        if forced:
            # The guardrail blocked an agent APPROVE in this run (read clears it at the start of every run).
            return {"final": forced}
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
    # Every other node routes itself with Command(goto=...), so any stage can divert to fail.
    for terminal in ("mark_duplicate", "log_rejection", "fail"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer)

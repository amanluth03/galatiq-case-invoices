"""Analyst <-> VP critique-loop subgraph: the agentic core of approval (validation -> approval -> guardrail).
Agents are injected as plain functions, so this graph is testable with fakes and has no LLM code in it.
"""
from typing import Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from invoice_agent.approval.policy import CHECKLIST_KEYS, checklist_complete
from invoice_agent.config import MAX_ROUNDS, SCRUTINY_THRESHOLD
from invoice_agent.models import (
    ApprovalOutcome, Flag, Invoice, Recommendation, Round, Uncertainty, VPReview,
)


class ApprovalContext(BaseModel):
    """Everything both agents get to see about the invoice under review."""
    invoice: Invoice
    flags: list[Flag] = []
    uncertainties: list[Uncertainty] = []
    policy: str = ""


# The analyst receives prior rounds so it can read the VP's feedback.
AnalystFn = Callable[[ApprovalContext, list[Round]], Recommendation]
VPFn = Callable[[ApprovalContext, Recommendation, list[Round]], VPReview]


class ApprovalState(TypedDict):
    """Subgraph state: the fixed context, the growing list of rounds, and the final outcome."""
    context: ApprovalContext
    rounds: list[Round]
    outcome: ApprovalOutcome | None


def _is_over_threshold(ctx: ApprovalContext) -> bool:
    """True when the invoice total is over the $10K scrutiny line (a missing total counts as not over)."""
    total = ctx.invoice.total
    return bool(total and total > SCRUTINY_THRESHOLD)


def _missing_keys(rec: Recommendation) -> list[str]:
    """Checklist keys the analyst left empty or blank."""
    checklist = rec.checklist or {}
    return [k for k in CHECKLIST_KEYS if not (checklist.get(k) or "").strip()]


def _append_feedback(review: VPReview, text: str) -> str:
    """Add a sentence to the VP's existing feedback (or start the feedback with it)."""
    return f"{review.feedback} {text}" if review.feedback else text


def make_analyst_node(analyst: AnalystFn):
    """Build the node that asks the analyst for a recommendation and opens a new round."""
    def analyst_node(state: ApprovalState) -> dict:
        """Call the analyst with prior rounds, append a new Round with no review yet."""
        rounds = state["rounds"]
        rec = analyst(state["context"], rounds)
        return {"rounds": [*rounds, Round(number=len(rounds) + 1, recommendation=rec, review=None)]}
    return analyst_node


def make_vp_node(vp: VPFn):
    """Build the node that asks the VP to review the latest recommendation."""
    def vp_node(state: ApprovalState) -> dict:
        """Call the VP, apply the code-enforced send-back rules, and store the review on the last round."""
        ctx, rounds = state["context"], state["rounds"]
        last = rounds[-1]
        review = vp(ctx, last.recommendation, rounds)
        if review.verdict == "ACCEPT":
            if review.final_decision is None:
                # An ACCEPT with no APPROVE/REJECT is unusable, so make the VP try again.
                review = review.model_copy(update={
                    "verdict": "SEND_BACK",
                    "feedback": _append_feedback(review, "VP accepted without a final decision"),
                })
            elif _is_over_threshold(ctx) and not checklist_complete(last.recommendation):
                # Policy: over $10K the VP cannot accept an incomplete checklist, even if it wants to.
                missing = ", ".join(_missing_keys(last.recommendation))
                review = review.model_copy(update={
                    "verdict": "SEND_BACK",
                    "feedback": _append_feedback(
                        review, f"Over $10K: checklist incomplete — missing: {missing}"),
                })
        # Copy the round rather than mutating state in place.
        new_rounds = [*rounds[:-1], last.model_copy(update={"review": review})]
        return {"rounds": new_rounds}
    return vp_node


def route_after_vp(state: ApprovalState) -> Literal["analyst", "finish"]:
    """Decide whether to loop back to the analyst or finish; reads state only, never changes it."""
    last, n = state["rounds"][-1], len(state["rounds"])
    over = _is_over_threshold(state["context"])
    accepted = last.review is not None and last.review.verdict == "ACCEPT"
    if accepted and over and not checklist_complete(last.recommendation):
        # Safety net: stays correct even if the VP node's enforcement was bypassed.
        return "analyst" if n < MAX_ROUNDS else "finish"
    if accepted:
        return "finish"
    return "analyst" if n < MAX_ROUNDS else "finish"


def _dedupe(items: list[str]) -> list[str]:
    """Remove duplicates, keeping first-seen order."""
    return list(dict.fromkeys(items))


def finish_node(state: ApprovalState) -> dict:
    """Turn the final round into an ApprovalOutcome: the VP's decision, or NEEDS_HUMAN_REVIEW."""
    rounds = state["rounds"]
    last = rounds[-1]
    review = last.review
    questions: list[str] = []
    for r in rounds:
        questions += r.recommendation.open_questions
        if r.review:
            questions += r.review.open_questions
    accepted = (
        review is not None and review.verdict == "ACCEPT" and review.final_decision is not None
        and (not _is_over_threshold(state["context"]) or checklist_complete(last.recommendation))
    )
    if accepted:
        decision, reasoning = review.final_decision, review.reasoning
    else:
        # The agents could not settle within the round limit, so a human decides.
        decision = "NEEDS_HUMAN_REVIEW"
        feedback = review.feedback if review else None
        reasoning = f"VP not satisfied after {MAX_ROUNDS} rounds: {feedback}"
    outcome = ApprovalOutcome(
        decision=decision, reasoning=reasoning, rounds=rounds, open_questions=_dedupe(questions),
    )
    return {"outcome": outcome}


def build_approval_graph(analyst: AnalystFn, vp: VPFn) -> CompiledStateGraph:
    """Wire analyst -> vp -> (analyst | finish) into a compiled subgraph."""
    g = StateGraph(ApprovalState)
    g.add_node("analyst", make_analyst_node(analyst))
    g.add_node("vp", make_vp_node(vp))
    g.add_node("finish", finish_node)
    g.add_edge(START, "analyst")
    g.add_edge("analyst", "vp")
    g.add_conditional_edges("vp", route_after_vp)
    g.add_edge("finish", END)
    return g.compile()

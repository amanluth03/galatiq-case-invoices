"""Tests for the analyst/VP critique-loop subgraph, driven by scripted fake agents."""
from decimal import Decimal

from invoice_agent.approval.policy import CHECKLIST_KEYS
from invoice_agent.approval.subgraph import ApprovalContext, build_approval_graph
from invoice_agent.models import Invoice
from tests.fakes import ScriptedAnalyst, ScriptedVP, make_recommendation, make_review

FULL = {k: "ok" for k in CHECKLIST_KEYS}


def run(analyst, vp, total="5000"):
    """Run the subgraph on an invoice with the given total and return the outcome."""
    ctx = ApprovalContext(invoice=Invoice(total=Decimal(total)), policy="p")
    out = build_approval_graph(analyst, vp).invoke({"context": ctx, "rounds": [], "outcome": None})
    return out["outcome"]


def send_back(fb="more evidence"):
    """A SEND_BACK review with feedback."""
    return make_review(verdict="SEND_BACK", final_decision=None, feedback=fb)


def test_accept_first_round_approve():
    """VP accepting round 1 with APPROVE ends the loop with APPROVE."""
    a, v = ScriptedAnalyst([make_recommendation()]), ScriptedVP([make_review()])
    o = run(a, v)
    assert o.decision == "APPROVE" and len(o.rounds) == 1
    assert len(a.calls) == 1 and len(v.calls) == 1


def test_accept_reject_is_a_valid_exit():
    """A well-argued REJECT accepted by the VP is a successful outcome."""
    a = ScriptedAnalyst([make_recommendation("REJECT")])
    v = ScriptedVP([make_review(final_decision="REJECT")])
    o = run(a, v)
    assert o.decision == "REJECT" and o.reasoning == "vp reasoning"
    assert len(a.calls) == 1


def test_send_back_then_accept():
    """After a send-back the analyst's 2nd call sees round 1 including the VP's feedback text."""
    a = ScriptedAnalyst([make_recommendation(), make_recommendation()])
    v = ScriptedVP([send_back("explain the price"), make_review()])
    o = run(a, v)
    assert o.decision == "APPROVE" and len(o.rounds) == 2
    assert len(a.calls) == 2 and len(v.calls) == 2
    assert a.calls[0][1] == []
    prior = a.calls[1][1]
    assert len(prior) == 1 and prior[0].review.feedback == "explain the price"


def test_three_send_backs_needs_human_review():
    """Three send-backs exhaust the rounds and hand off to a human."""
    a = ScriptedAnalyst([make_recommendation() for _ in range(3)])
    v = ScriptedVP([send_back("one"), send_back("two"), send_back("three")])
    o = run(a, v)
    assert o.decision == "NEEDS_HUMAN_REVIEW"
    assert o.reasoning == "VP not satisfied after 3 rounds: three"
    assert len(a.calls) == 3 and len(v.calls) == 3 and len(o.rounds) == 3


def test_over_10k_incomplete_checklist_routed_back_even_if_vp_accepts():
    """Over $10K, an ACCEPT on an incomplete checklist is converted to SEND_BACK naming the gap."""
    partial = {k: "ok" for k in CHECKLIST_KEYS if k != "arithmetic"}
    a = ScriptedAnalyst([make_recommendation(checklist=partial), make_recommendation(checklist=FULL)])
    v = ScriptedVP([make_review(), make_review()])
    o = run(a, v, total="15000")
    assert o.decision == "APPROVE" and len(o.rounds) == 2
    assert o.rounds[0].review.verdict == "SEND_BACK"
    assert "arithmetic" in o.rounds[0].review.feedback
    assert "arithmetic" in a.calls[1][1][0].review.feedback
    assert len(a.calls) == 2 and len(v.calls) == 2


def test_over_10k_incomplete_checklist_all_rounds_needs_human_review():
    """If the checklist never completes over $10K, the loop ends in NEEDS_HUMAN_REVIEW."""
    a = ScriptedAnalyst([make_recommendation() for _ in range(3)])
    v = ScriptedVP([make_review() for _ in range(3)])
    o = run(a, v, total="15000")
    assert o.decision == "NEEDS_HUMAN_REVIEW"
    assert len(a.calls) == 3 and "checklist incomplete" in o.reasoning


def test_under_10k_needs_no_checklist():
    """At or under $10K an accepted recommendation needs no checklist."""
    a, v = ScriptedAnalyst([make_recommendation(checklist=None)]), ScriptedVP([make_review()])
    o = run(a, v, total="10000")
    assert o.decision == "APPROVE" and len(a.calls) == 1


def test_accept_without_final_decision_is_sent_back():
    """An ACCEPT lacking a final decision is stored as SEND_BACK and the analyst retries."""
    a = ScriptedAnalyst([make_recommendation(), make_recommendation()])
    v = ScriptedVP([make_review(final_decision=None), make_review()])
    o = run(a, v)
    assert o.rounds[0].review.verdict == "SEND_BACK"
    assert "without a final decision" in o.rounds[0].review.feedback
    assert o.decision == "APPROVE" and len(a.calls) == 2


def test_open_questions_deduplicated_in_order():
    """Open questions from all recommendations and reviews are merged without duplicates."""
    a = ScriptedAnalyst([make_recommendation(open_questions=["q1", "q2"]),
                         make_recommendation(open_questions=["q2", "q3"])])
    v = ScriptedVP([make_review(verdict="SEND_BACK", final_decision=None, feedback="f", open_questions=["q4", "q1"]),
                    make_review(open_questions=["q5"])])
    o = run(a, v)
    assert o.open_questions == ["q1", "q2", "q4", "q3", "q5"]
    assert len(a.calls) == 2

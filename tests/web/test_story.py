"""Tests for the invoice story builder (web/story.py): one test per outcome (paid, rule-rejected, agent-rejected,
waiting for a person, duplicate, error, multi-round send-back), and that the live story ends where the finished one does.
States come from real pipeline runs over fake agents, or are built by hand where a run would need the real LLM.
"""
import shutil
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps, build_graph, make_serializer
from invoice_agent.database import db
from invoice_agent.models import (
    ApprovalOutcome, FinalOutcome, Flag, Invoice, LineItem, Round, Uncertainty,
)
from invoice_agent.runner import process_file
from invoice_agent.utils.guardrail import HUMAN_REVIEW_REASON
from invoice_agent.web.app import progress_recorder, row_state
from invoice_agent.web.story import (
    SKIPPED_AGENTS, SLOW_STEP_HINT, build_story, field_words, lookup_text, plain_message,
)
from tests.fakes import ScriptedAnalyst, ScriptedIngestionAgent, ScriptedVP, make_extracted, make_recommendation, make_review
from tests.test_graph import extracted_1002
from tests.test_runner import INVOICES, RecordingPay, build

FULL_CHECKLIST = {k: "checked" for k in CHECKLIST_KEYS}


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded app database for each test."""
    c = db.connect(tmp_path / "app.db")
    db.init_db(c)
    yield c
    c.close()


def finished_story(conn, run_id, original_name=None):
    """The story of a saved, finished run, as its page builds it."""
    row = db.get_run(conn, run_id)
    return build_story(row_state(row), row["stage"], False, original_name=original_name, decided_at=row["updated_at"])


def texts(step):
    """All line texts of one step, joined, for easy assertions."""
    return " | ".join(line.text for line in step.lines)


def states(steps):
    """Each step's state, in order."""
    return [s.state for s in steps]


def test_paid_story(conn):
    """A clean invoice reads as 7 finished steps ending in "Paid $5,000.00 to Widgets Inc."."""
    c = conn
    deps, graph, _ = build(c, [make_extracted()])
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    steps = finished_story(c, state["run_id"])
    assert [s.number for s in steps] == [1, 2, 3, 4, 5, 6, 7]
    assert states(steps) == ["done"] * 7
    assert texts(steps[0]).startswith("Text invoice, ")
    assert texts(steps[1]) == "Widgets Inc., $5,000.00 | 2 items, due 1 Feb 2026"
    assert texts(steps[2]) == "Everything checks out"
    assert texts(steps[3]).startswith("Recommends approving")
    assert texts(steps[4]).startswith("Agrees: approve")
    assert texts(steps[5]) == "All clear: no hard rules broken."
    assert steps[6].title == "Decision: Paid" and texts(steps[6]).startswith("Paid $5,000.00 to Widgets Inc.")
    assert "Decided by the analyst and VP agents" in texts(steps[6])


def test_rule_rejected_story(conn):
    """An over-stock invoice is rejected by a hard rule straight after the checks: steps 4 and 5 are skipped
    (the agents weren't needed), step 6 names the rule and step 7 says why it was rejected."""
    deps, graph, _ = build(conn, [extracted_1002()], checklist=FULL_CHECKLIST)
    state = process_file(INVOICES / "invoice_1002.txt", deps, graph)
    steps = finished_story(conn, state["run_id"])
    assert steps[2].state == "warn" and "All items are in stock" not in texts(steps[2])
    assert "Ordered 20 GadgetX, only 5 in stock" in texts(steps[2])
    assert steps[3].state == steps[4].state == "skipped"
    assert texts(steps[3]) == SKIPPED_AGENTS == texts(steps[4])
    assert steps[5].lines[0].kind == "block"
    assert texts(steps[5]) == "A hard rule is broken (1 item is ordered beyond stock on hand), so it's rejected without the agents."
    assert steps[6].state == "failed" and "Rejected: 1 item is ordered beyond stock on hand." in texts(steps[6])
    assert "the guardrail" in texts(steps[6])


def test_agent_rejected_story(conn):
    """When the analyst and VP agree to reject a clean invoice, the guardrail says their rejection stands."""
    deps = Deps(ingestion=ScriptedIngestionAgent([make_extracted()]),
                analyst=ScriptedAnalyst([make_recommendation("REJECT", reasoning="No PO on file. Vendor unknown.")]),
                vp=ScriptedVP([make_review(final_decision="REJECT", reasoning="Agreed, no PO.")]),
                pay=RecordingPay(), conn=conn)
    graph = build_graph(deps, InMemorySaver(serde=make_serializer()))
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    steps = finished_story(conn, state["run_id"])
    assert texts(steps[3]) == "Recommends rejecting | No PO on file. Vendor unknown."
    assert texts(steps[4]) == "Agrees: reject | Agreed, no PO."
    assert texts(steps[5]) == "No hard rules broken. The agents' rejection stands."
    assert texts(steps[6]).startswith("Rejected: Agreed, no PO.")


def test_multi_round_send_back_waits_for_a_person(conn):
    """Three send-backs show as Round 2 and Round 3 under the VP step, and the run waits for a person."""
    deps, graph, _ = build(conn, [make_extracted()], send_back=True)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    steps = finished_story(conn, state["run_id"])
    vp = [(l.kind, l.text) for l in steps[4].lines]
    assert vp[0] == ("agent", "Sends it back: again")
    assert ("round", "Round 2") in vp and ("round", "Round 3") in vp and ("round", "Round 4") not in vp
    assert "the agents couldn't agree after 3 rounds, so a person must decide" in texts(steps[5])
    assert steps[6].title == "Decision: Waiting for you" and steps[6].state == "warn"
    assert texts(steps[6]).startswith("Waiting for you: nothing has been paid.")


def test_money_difference_waits_for_a_person_with_plain_reason():
    """A price differing from Acme's records reads "WidgetA's price differs ..., so a person must decide"."""
    inv = Invoice(invoice_number="INV-1010", vendor="Consolidated Materials Group", total=Decimal("7185.00"),
                  currency="EUR", items=[LineItem(name_raw="WidgetA", sku="WidgetA", qty=Decimal(1),
                                                  unit_price=Decimal("300.00"))])
    flags = [Flag(severity="needs_scrutiny", code="price_mismatch", field_path="items[0].unit_price",
                  message="WidgetA charged $300.00, Acme's price is $250.00")]
    rounds = [Round(number=1, recommendation=make_recommendation("REJECT"), review=make_review(final_decision="REJECT"))]
    state = {
        "raw_text": "a\nb", "format": "txt", "invoice": inv, "fingerprint": "fp", "flags": flags,
        "uncertainties": [Uncertainty(field_path="due_date", confidence="low", reason="x")],
        "approval": ApprovalOutcome(decision="NEEDS_HUMAN_REVIEW", rounds=rounds,
                                    reasoning=HUMAN_REVIEW_REASON.format(recommendation="REJECT")),
        "final": FinalOutcome(status="NEEDS_HUMAN_REVIEW", decided_by="agents", reasoning="held"),
    }
    steps = build_story(state, "human_review", False)
    assert "Amounts are in EUR and were not converted." in texts(steps[1])
    assert "Unsure about: due date" in texts(steps[1]) and steps[1].state == "warn"
    assert texts(steps[5]) == ("No hard rules broken, but WidgetA's price differs from Acme's records, "
                               "so a person must decide.")
    assert "The agents recommended rejecting." in texts(steps[6])


def test_duplicate_story_ends_at_step_three(conn, tmp_path):
    """A second copy of a processed invoice says which file it duplicates and skips the agents and guardrail."""
    deps, graph, _ = build(conn, [make_extracted(), make_extracted()])
    first = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    copy = tmp_path / "copy_1001.txt"
    # Different bytes (so not the same file), same invoice contents: a duplicate.
    shutil.copy(INVOICES / "invoice_1001.txt", copy)
    copy.write_text(copy.read_text() + "\n")
    second = process_file(copy, deps, graph)
    assert second["final"].status == "DUPLICATE" and second["duplicate_of"] == first["run_id"]
    steps = finished_story(conn, second["run_id"], original_name="invoice_1001.txt")
    assert texts(steps[2]) == ("This invoice was already processed (as invoice_1001.txt), "
                               "so it was not processed or paid again.")
    assert states(steps)[3:6] == ["skipped"] * 3
    assert steps[6].title == "Decision: Duplicate"


def test_error_story_fails_at_the_step_that_broke(conn):
    """An ingestion failure marks step 2 failed with the reason, skips 3 to 6 and stops at the decision."""
    deps, graph, _ = build(conn, [RuntimeError("model said no")] * 3)
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph)
    steps = finished_story(conn, state["run_id"])
    assert states(steps) == ["done", "failed", "skipped", "skipped", "skipped", "skipped", "failed"]
    assert "model said no" in texts(steps[1])
    assert "nothing was paid" in texts(steps[6])


def test_unreadable_file_fails_at_step_one(conn, tmp_path):
    """A corrupt PDF stops at step 1 with the reader's reason."""
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    deps, graph, _ = build(conn, [])
    state = process_file(bad, deps, graph)
    steps = finished_story(conn, state["run_id"])
    assert steps[0].state == "failed" and steps[0].lines[0].kind == "block"


def test_live_story_grows_and_ends_like_the_finished_one(conn):
    """While a run is PROCESSING each node adds to the story (rounds included); its last live steps 1 to 6
    are exactly the finished story's, so live and finished pages never disagree."""
    deps, graph, _ = build(conn, [make_extracted()], send_back=True)
    db.create_pending_run(conn, "r1", str(INVOICES / "invoice_1001.txt"))
    record = progress_recorder(conn, "r1")
    snapshots = []

    def on_update(namespace, node, update):
        """Save progress like the background job, then capture the live story as the screen would show it."""
        record(namespace, node, update)
        row = db.get_run(conn, "r1")
        snapshots.append(build_story(row_state(row), row["stage"], True))

    process_file(INVOICES / "invoice_1001.txt", deps, graph, run_id="r1", on_update=on_update)
    # After read, step 2 is the running one.
    assert states(snapshots[0])[:2] == ["done", "running"]
    # Mid-loop the VP step is running and already shows the second round.
    mid = next(s for s in snapshots if s[4].state == "running" and "Round 2" in texts(s[4]))
    assert mid[3].state == "done"
    final = finished_story(conn, "r1")
    assert snapshots[-1][:6] == final[:6]
    assert snapshots[-1][6].state == "running"


def test_queued_story_shows_nothing_running():
    """A run waiting its turn in a batch shows all 7 steps pending, and ignores any old result in its row."""
    old = {"final": FinalOutcome(status="REJECTED", decided_by="agents", reasoning="old"), "raw_text": "old"}
    steps = build_story(old, "queued", True)
    assert states(steps) == ["pending"] * 7


def test_plain_words_for_paths_and_lookups():
    """Field paths, flag messages and tool calls read as plain words, never codes."""
    assert field_words("items[1].unit_price") == "line 2 unit price" and field_words("due_date") == "due date"
    assert plain_message("items[0]: line_total 10 is more than qty x unit_price = 5") == \
        "Line 1: line total 10 is more than quantity × unit price = 5"
    assert lookup_text("get_inventory", {"item": "WidgetA"}) == "Looked up WidgetA stock and price"
    assert lookup_text("Recommendation", {}) is None


def _blocked_state(final: FinalOutcome, **extra) -> dict:
    """A validated invoice with one broken hard rule (unknown item) that reached the agents, ending in `final`."""
    inv = Invoice(invoice_number="INV-9", vendor="Widgets Inc.", total=Decimal("100.00"),
                  items=[LineItem(name_raw="Gizmo", qty=Decimal(1), unit_price=Decimal("100.00"))])
    rounds = [Round(number=1, recommendation=make_recommendation("APPROVE"), review=make_review())]
    return {"raw_text": "x", "format": "txt", "invoice": inv, "fingerprint": "fp",
            "flags": [Flag(severity="must_reject", code="unknown_item", message="Gizmo isn't in inventory")],
            "approval": ApprovalOutcome(decision="NEEDS_HUMAN_REVIEW", rounds=rounds, reasoning="held"),
            "final": final, **extra}


def test_guardrail_step_matches_what_happened_to_a_rule_breaking_invoice():
    """Step 6 only says "rejected" when it was: a person's override (with their justification) and a run still
    waiting for a person each get their own sentence."""
    from invoice_agent.models import HumanDecision
    waiting = build_story(_blocked_state(FinalOutcome(status="NEEDS_HUMAN_REVIEW", decided_by="agents",
                                                      reasoning="held")), "human_review", False)
    assert texts(waiting[5]) == ("A hard rule is broken (1 item isn't in inventory), "
                                 "so it's waiting for a person to decide.")
    overridden = build_story(_blocked_state(
        FinalOutcome(status="PAID", decided_by="human", reasoning="ok"),
        human_decision=HumanDecision(decision="APPROVE", justification="New SKU agreed by phone.")), "pay", False)
    assert texts(overridden[5]) == ("A hard rule is broken (1 item isn't in inventory), but a person overrode it "
                                    "with this justification: New SKU agreed by phone.")
    assert "rejected" not in texts(overridden[5]) and "rejected" not in texts(waiting[5])


def test_pdf_step_one_counts_pages():
    """A PDF with a page count reads "PDF, 2 pages"; one saved before page counts existed still shows its lines."""
    assert texts(build_story({"raw_text": "a\nb\nc", "format": "pdf", "page_count": 2}, "read", True)[0]) == \
        "PDF, 2 pages"
    assert texts(build_story({"raw_text": "a\nb\nc", "format": "pdf"}, "read", True)[0]) == "PDF, 3 lines"


def test_slow_running_step_says_grok_is_retrying():
    """A step running for more than 30 seconds gets the calm "taking longer than usual" line; a fresh one doesn't."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    state = {"raw_text": "a", "format": "txt"}
    slow = build_story({**state, "step_started_at": (now - timedelta(seconds=45)).isoformat()}, "read", True, now=now)
    fresh = build_story({**state, "step_started_at": (now - timedelta(seconds=5)).isoformat()}, "read", True, now=now)
    assert slow[1].state == "running" and SLOW_STEP_HINT in texts(slow[1])
    assert SLOW_STEP_HINT not in texts(fresh[1])

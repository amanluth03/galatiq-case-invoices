"""Tests for the review brief builder: what a person sees when a run waits at human review (the last pipeline step)."""
from decimal import Decimal

from invoice_agent.models import (ApprovalOutcome, FinalOutcome, Flag, Invoice, LineItem, Recommendation, Round,
                                  Uncertainty, VPReview)
from invoice_agent.utils.review_brief import build_review_brief

D = Decimal


def waiting_final():
    """The outcome of a run held for a person."""
    return FinalOutcome(status="NEEDS_HUMAN_REVIEW", decided_by="guardrail",
                        reasoning="Prices, totals or currency differ from Acme's records. The agents recommended APPROVE; a person decides.")


def approval(decision="APPROVE", rounds=1, questions=()):
    """An approval outcome with the given number of analyst/VP rounds."""
    rs = [Round(number=n + 1,
                recommendation=Recommendation(decision="APPROVE", reasoning="Analyst says the rush fee looks legitimate. Prices otherwise match.",
                                              open_questions=list(questions)),
                review=VPReview(verdict="ACCEPT" if decision != "NEEDS_HUMAN_REVIEW" else "SEND_BACK",
                                final_decision="APPROVE" if decision != "NEEDS_HUMAN_REVIEW" else None,
                                reasoning="The rush surcharge is noted on the line. First point. Second point. Third point."))
          for n in range(rounds)]
    return ApprovalOutcome(decision=decision, reasoning="r", rounds=rs, open_questions=list(questions))


def rush_state():
    """A 1010-like invoice: WidgetA billed at $300 instead of Acme's $250."""
    inv = Invoice(invoice_number="1010", vendor="Consolidated Materials Group", currency="USD", total=D("7185.00"),
                  items=[LineItem(name_raw="WidgetA (rush)", sku="WidgetA", qty=D("4"), unit_price=D("300.00"), line_total=D("1200.00"))])
    flags = [Flag(severity="needs_scrutiny", code="price_mismatch", field_path="items[0].unit_price",
                  message="WidgetA charged $300.00, Acme's price is $250.00 (line note: rush)")]
    unc = [Uncertainty(field_path="items[0].unit_price", raw="$300.00", normalized="300.00", confidence="medium", reason="smudged")]
    return {"invoice": inv, "flags": flags, "uncertainties": unc, "approval": approval(questions=["Was the rush fee agreed?"]),
            "final": waiting_final()}


def eur_state():
    """A 1014-like invoice in EUR with price differences and a wrong total."""
    inv = Invoice(vendor="Euro Parts", currency="EUR", total=D("1100.00"), tax=D("0"),
                  items=[LineItem(name_raw="WidgetA", sku="WidgetA", qty=D("2"), unit_price=D("300.00")),
                         LineItem(name_raw="WidgetB", sku="WidgetB", qty=D("1"), unit_price=D("400.00"))])
    flags = [
        Flag(severity="needs_scrutiny", code="non_usd_currency", field_path="currency", message="Invoice is in EUR, not US dollars"),
        Flag(severity="needs_scrutiny", code="price_mismatch", field_path="items[0].unit_price", message="WidgetA charged 300.00 EUR, Acme's price is $250.00"),
        Flag(severity="needs_scrutiny", code="price_mismatch", field_path="items[1].unit_price", message="WidgetB charged 400.00 EUR, Acme's price is $500.00"),
        Flag(severity="needs_scrutiny", code="arithmetic_mismatch", field_path="total", message="total: expected 1000, found 1100"),
    ]
    return {"invoice": inv, "flags": flags, "uncertainties": [], "approval": approval(), "final": waiting_final()}


def test_rush_price_brief():
    """A rush-price invoice names the price difference, links the check to the line and ends with an ok item."""
    brief = build_review_brief(rush_state())
    assert brief.headline == "Waiting for you: $7,185.00 to Consolidated Materials Group"
    assert brief.why == ["WidgetA is charged $300.00 on one line; Acme's price is $250.00."]
    assert brief.agents_recommended == "APPROVE"
    assert brief.agents_summary == "The rush surcharge is noted on the line. First point."
    price = brief.checks[0]
    assert price.text == ("Was the $300.00 price for WidgetA agreed? Acme's price is $250.00 "
                          "(difference $50.00 per unit, $200.00 on this line).")
    assert price.field_path == "items[0].unit_price" and price.snippet == "$300.00"
    assert any(c.text == "Was the rush fee agreed?" for c in brief.checks)
    assert brief.checks[-1].ok and not any(c.ok for c in brief.checks[:-1])
    assert brief.if_approve == "$7,185.00 is paid to Consolidated Materials Group."
    assert brief.if_reject == "Nothing is paid; the invoice is logged as rejected with your note."


def test_open_questions_are_not_repeated():
    """The same question raised by overall approval and by a round appears once as a check."""
    brief = build_review_brief(rush_state())
    assert [c.text for c in brief.checks].count("Was the rush fee agreed?") == 1


def test_eur_brief_explains_currency_and_totals():
    """A EUR invoice asks for the exchange rate and does not subtract EUR from Acme's USD prices."""
    brief = build_review_brief(eur_state())
    assert len(brief.why) <= 3
    assert brief.why[0] == "The invoice is in EUR; amounts were not converted to USD."
    assert brief.why[1] == "The printed total (1,100.00 EUR) doesn't match the line items plus tax (1,000.00 EUR)."
    texts = [c.text for c in brief.checks]
    assert texts[0] == "Confirm the EUR amounts and the exchange rate with finance before paying."
    assert "Check the totals: the lines add up to 1,000.00 EUR, the invoice says 1,100.00 EUR." in texts
    assert "Was the 300.00 EUR price for WidgetA agreed? Acme's price is $250.00 (in USD)." in texts
    assert not any("per unit" in t for t in texts)


def test_agents_disagree_brief():
    """When the agents deadlock, the brief says so, gives no recommendation and still summarises their last view."""
    state = rush_state()
    state["approval"] = approval(decision="NEEDS_HUMAN_REVIEW", rounds=3)
    brief = build_review_brief(state)
    assert brief.why[0] == "The agents couldn't agree after 3 rounds."
    assert brief.agents_recommended is None
    assert brief.agents_summary


def test_paused_run_without_final_still_gets_a_brief():
    """A run paused at human review (no final yet) is also waiting."""
    state = rush_state()
    del state["final"]
    state["approval"] = approval(decision="NEEDS_HUMAN_REVIEW", rounds=3)
    assert build_review_brief(state) is not None


def test_non_waiting_state_returns_none():
    """Paid, rejected or in-progress runs have no brief."""
    state = rush_state()
    state["final"] = FinalOutcome(status="PAID", decided_by="agents", reasoning="ok")
    assert build_review_brief(state) is None
    assert build_review_brief({"invoice": state["invoice"], "flags": []}) is None


def test_low_confidence_reading_becomes_a_check_and_other_flags_are_phrased():
    """Uncertain readings, big totals, due dates and unprinted amounts each get a plain check with a jump target."""
    state = rush_state()
    state["flags"] += [
        Flag(severity="needs_scrutiny", code="over_10k", field_path="total", message="Total is over"),
        Flag(severity="needs_scrutiny", code="due_before_or_on_invoice_date", field_path="due_date", message="Due date"),
        Flag(severity="needs_scrutiny", code="amount_not_in_source", field_path="total", message="total: x isn't printed"),
    ]
    state["uncertainties"].append(Uncertainty(field_path="invoice_number", raw="10l0", normalized="1010", confidence="low", reason="l read as 1"))
    brief = build_review_brief(state)
    texts = {c.text: c for c in brief.checks}
    assert texts["Large invoice: confirm the vendor and purchase order."].field_path == "total"
    assert "Check the due date: it is on or before the invoice date." in texts
    assert "Find $7,185.00 on the original document; the reader couldn't see it printed." in texts
    confirm = texts["Confirm the invoice number reads '10l0'."]
    assert confirm.field_path == "invoice_number" and confirm.snippet == "10l0"


def test_no_flag_codes_or_em_dashes_in_any_text():
    """Every string a reviewer sees is free of flag codes and the em dash, even when agent text contains one."""
    state = rush_state()
    state["approval"].rounds[0].review.reasoning = "Fine \u2014 the rush fee is noted. Done."
    for s in (rush_state(), eur_state(), state):
        brief = build_review_brief(s)
        strings = [brief.headline, *brief.why, brief.agents_summary or "", brief.if_approve, brief.if_reject,
                   *[c.text for c in brief.checks]]
        for text in strings:
            assert "\u2014" not in text
            for code in ("price_mismatch", "non_usd_currency", "arithmetic_mismatch", "amount_not_in_source", "over_10k"):
                assert code not in text

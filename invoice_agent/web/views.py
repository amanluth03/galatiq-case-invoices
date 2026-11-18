"""View models for the review screen's pages, already formatted for display (no raw codes or JSON reach a template):
the home dashboard (summary tiles and the table of every invoice) and an invoice page's "Invoice details" section
(original document, extracted fields, flags, over-$10K checklist, open questions and the analyst/VP exchange).
"""
import re
from decimal import Decimal, InvalidOperation

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.models import Flag, Uncertainty
from invoice_agent.utils.formatting import money, nice_date, quantity
from invoice_agent.web.highlight import highlight
from invoice_agent.web.story import (
    NOT_STARTED, QUEUED, STATUS_LABELS, blocking_summary, display_name, nice_time, plain_message, short,
)

SEVERITY_GROUPS = [
    ("must_reject", "Blocking", "Any one of these stops payment."),
    ("needs_scrutiny", "Needs a look", "Allowed only with a clear explanation."),
    ("info", "FYI", "Context: not decisive on its own."),
]
CONFIDENCE_LABELS = {"high": "Confident", "medium": "Somewhat sure", "low": "Unsure"}
CHECKLIST_LABELS = {
    "vendor_history": "Vendor history", "price_check": "Prices vs. our records", "stock_check": "Stock on hand",
    "arithmetic": "Math reconciles", "payment_terms": "Payment terms",
}
# Short plain-language titles for each validation flag code (the codes themselves never reach the screen).
FLAG_TITLES = {
    "unknown_item": "Item not in inventory", "zero_stock_item": "Item has zero stock",
    "exceeds_stock": "More than we have in stock", "non_positive_quantity": "Zero or negative quantity",
    "missing_quantity": "Quantity missing", "negative_total": "Negative total", "missing_vendor": "Vendor missing",
    "missing_total": "Total missing", "no_line_items": "No line items",
    "total_exceeds_line_items": "Total is more than the lines add up to",
    "line_total_exceeds_price": "Line total is more than quantity × price",
    "total_unverifiable": "Total can't be verified",
    "invoice_number_reused": "Invoice number already billed", "price_mismatch": "Price differs from our records",
    "arithmetic_mismatch": "Numbers don't reconcile", "over_10k": "Over $10,000: extra checks required",
    "due_date_missing": "Due date missing", "due_before_or_on_invoice_date": "Due on or before the invoice date",
    "non_usd_currency": "Not in US dollars", "missing_unit_price": "Unit price missing",
    "amount_not_in_source": "Amount not found on the invoice",
    "extraction_issue": "The reader was unsure", "urgency_language": "Payment-pressure language",
    "vendor_name_change": "Vendor name changed",
}
# Invoice header fields shown in the extracted-fields table, in reading order: (field, label, kind).
FIELDS = [
    ("invoice_number", "Invoice number", "text"), ("vendor", "Vendor", "text"), ("date", "Invoice date", "date"),
    ("due_date", "Due date", "date"), ("payment_terms", "Payment terms", "text"), ("currency", "Currency", "text"),
    ("subtotal", "Subtotal", "money"), ("tax", "Tax", "money"), ("shipping", "Shipping", "money"),
    ("total", "Total", "money"), ("notes", "Notes", "text"),
]
# Dashboard order: waiting for a person first, then running (and queued), then everything else newest first.
_SORT_RANK = {"NEEDS_HUMAN_REVIEW": 0, "PROCESSING": 1}


# ---------- small helpers ----------

def humanize_reason(u: Uncertainty) -> str:
    """Turn an uncertainty's reason into reviewer-friendly words (for the fields table and the highlight tooltips)."""
    if u.reason.startswith("ingestion agent confidence"):
        return "The reader wasn't fully sure of this value." if u.confidence == "medium" else \
            "The reader was unsure of this value."
    return u.reason[0].upper() + u.reason[1:] if u.reason else ""


def _amount(value) -> Decimal:
    """A stored amount as a Decimal (0 when missing or unreadable), for the dashboard's dollar totals."""
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _worst_severity(flags: list[Flag]) -> str | None:
    """The most serious severity among flags, for marking a line item row."""
    for sev, _, _ in SEVERITY_GROUPS:
        if any(f.severity == sev for f in flags):
            return sev
    return None


def sort_rows(rows: list[dict]) -> list[dict]:
    """Runs in dashboard order: waiting for you, then running, then newest first."""
    newest = sorted(rows, key=lambda r: r.get("updated_at") or "", reverse=True)
    return sorted(newest, key=lambda r: _SORT_RANK.get(r["status"] or "ERROR", 2))


# ---------- home dashboard ----------

def why_line(state: dict, status: str, running_title: str | None) -> str:
    """The dashboard's one-line "why" for a run: what it is doing now, or why it ended the way it did."""
    if status == "PROCESSING":
        return running_title or "Waiting its turn"
    final = state.get("final")
    if final is None:
        return ""
    flags = state.get("flags") or []
    blocked = blocking_summary(flags)
    if final.status == "NEEDS_HUMAN_REVIEW":
        approval = state.get("approval")
        if blocked:
            return f"A hard rule is broken ({blocked}): your call"
        if approval and approval.reasoning.startswith("Prices, totals or currency differ"):
            return "Prices, totals or currency differ from Acme's records: your call"
        n = len(approval.rounds) if approval else 0
        return f"The agents couldn't agree after {n} round{'s' if n != 1 else ''}: your call"
    if final.decided_by == "human":
        verb = "Approved" if final.status == "PAID" else "Rejected"
        return f"{verb} by a reviewer" + (f": {short(final.human_note, 1, 90)}" if final.human_note else "")
    if final.status == "REJECTED" and blocked:
        return f"Hard rule: {blocked}"
    if final.status == "DUPLICATE":
        return "Already processed: not paid twice"
    if final.status == "ERROR":
        prefix = "Processing failed: " if state.get("raw_text") else "Couldn't read this file: "
        return prefix + short(final.reasoning, 1, 90)
    return short(final.reasoning, 1, 120)


def dashboard_view(rows: list[dict], states: dict[str, dict], running_titles: dict[str, str | None]) -> dict:
    """Summary tiles (counts and dollar totals) and the table rows for the home page.
    states: each run's hydrated state by run id; running_titles: the running step's title for PROCESSING runs."""
    counts = {k: 0 for k in ("PAID", "REJECTED", "NEEDS_HUMAN_REVIEW", "DUPLICATE", "ERROR", "PROCESSING")}
    totals = {k: Decimal(0) for k in ("PAID", "REJECTED", "NEEDS_HUMAN_REVIEW")}
    non_usd = False
    items = []
    for row in sort_rows(rows):
        status = row["status"] or "ERROR"
        state = states[row["run_id"]]
        live = status == "PROCESSING"
        # A re-run's row holds its previous result until the first step finishes: show none of it yet.
        inv = None if live and row.get("stage") in NOT_STARTED else state.get("invoice")
        currency = inv.currency if inv else "USD"
        counts[status] = counts.get(status, 0) + 1
        if status in totals and row["amount"]:
            # Dollar totals only add up USD invoices: a EUR amount summed as dollars would mislead.
            if currency.upper() == "USD":
                totals[status] += _amount(row["amount"])
            else:
                non_usd = True
        queued = live and row.get("stage") == QUEUED
        items.append({
            "run_id": row["run_id"], "name": display_name(row["run_id"], row["source_path"]) or row["run_id"],
            "vendor": (inv.vendor if inv else None) or "", "status": status.lower(),
            "label": "Queued" if queued else STATUS_LABELS.get(status, status),
            "amount": money(inv.total, currency) if inv and inv.total is not None else "",
            "why": "Waiting its turn" if queued else why_line(state, status, running_titles.get(row["run_id"])),
            "time": nice_time(row.get("updated_at")), "live": live,
        })
    tiles = [
        {"key": "needs_human_review", "label": "Waiting for you", "count": counts["NEEDS_HUMAN_REVIEW"],
         "amount": money(totals["NEEDS_HUMAN_REVIEW"]) + " at stake", "highlight": counts["NEEDS_HUMAN_REVIEW"] > 0},
        {"key": "paid", "label": "Paid", "count": counts["PAID"], "amount": money(totals["PAID"])},
        {"key": "rejected", "label": "Rejected", "count": counts["REJECTED"], "amount": money(totals["REJECTED"])},
        {"key": "duplicate", "label": "Duplicates", "count": counts["DUPLICATE"], "amount": None},
        {"key": "error", "label": "Errors", "count": counts["ERROR"], "amount": None},
        {"key": "processing", "label": "Running", "count": counts["PROCESSING"], "amount": None},
    ]
    return {"tiles": tiles, "rows": items, "running": counts["PROCESSING"] > 0, "non_usd": non_usd}


# ---------- invoice details ----------

def details_view(state: dict) -> dict:
    """The "Invoice details" section under an invoice's story: every part that exists so far (live runs fill in)."""
    inv, approval = state.get("invoice"), state.get("approval")
    flags, uncs = state.get("flags") or [], state.get("uncertainties") or []
    currency = inv.currency if inv else "USD"
    by_path = {u.field_path: u for u in uncs}

    def unc_view(path: str) -> dict | None:
        """The uncertainty on a field, formatted, or None if the reader was confident."""
        u = by_path.get(path)
        if u is None or u.confidence == "high":
            return None
        return {"confidence": u.confidence, "label": CONFIDENCE_LABELS[u.confidence], "raw": u.raw,
                "reason": humanize_reason(u)}

    fields = []
    for key, label, kind in FIELDS if inv else []:
        value = getattr(inv, key, None)
        if key in ("shipping", "notes") and value is None and key not in by_path:
            continue  # optional fields most invoices don't have: hide rather than show a row of "n/a"
        shown = money(value, currency) if kind == "money" else nice_date(value) if kind == "date" else (value or "n/a")
        fields.append({"label": label, "value": shown, "money": kind == "money", "unc": unc_view(key)})

    lines = []
    for i, it in enumerate(inv.items if inv else []):
        line_flags = [f for f in flags if re.match(rf"items\[{i}\](\.|$)", f.field_path or "")]
        lines.append({
            "name": it.name_raw, "sku": it.sku if it.sku and it.sku != it.name_raw else None,
            "suggestion": it.sku_suggestion, "qty": quantity(it.qty), "unit_price": money(it.unit_price, currency),
            "line_total": money(it.line_total, currency), "note": it.note, "severity": _worst_severity(line_flags),
            "unc": {k: unc_view(f"items[{i}].{k}") for k in ("name", "qty", "unit_price", "line_total")},
        })

    groups = []
    for sev, title, hint in SEVERITY_GROUPS:
        # Messages are plain English already; plain_message only spells out any field path ("items[0]" -> "line 1"),
        # exactly as the story's step 3 does.
        members = [{"title": FLAG_TITLES.get(f.code, f.code.replace("_", " ").capitalize()),
                    "message": plain_message(f.message)}
                   for f in flags if f.severity == sev]
        if members:
            groups.append({"severity": sev, "title": title, "hint": hint, "flags": members})

    rounds, checklist = [], None
    for r in approval.rounds if approval else list(state.get("live_rounds") or []):
        rec, rev = r.recommendation, r.review
        rounds.append({
            "number": r.number, "decision": rec.decision, "reasoning": rec.reasoning, "evidence": rec.evidence,
            "review": None if rev is None else {
                "accepted": rev.verdict == "ACCEPT", "final_decision": rev.final_decision,
                "reasoning": rev.reasoning, "feedback": rev.feedback,
            },
        })
        if rec.checklist:
            # The latest round's checklist is the one the VP judged.
            checklist = [{"label": CHECKLIST_LABELS.get(k, k), "answer": (rec.checklist.get(k) or "").strip()}
                         for k in CHECKLIST_KEYS]

    raw_text = state.get("raw_text")
    marks = [u.model_copy(update={"reason": humanize_reason(u)}) for u in uncs if u.confidence != "high"]
    final = state.get("final")
    return {
        "source": highlight(raw_text, marks) if raw_text else None,
        "uncertain_count": len(marks), "read": raw_text is not None, "extracted": inv is not None,
        "validated": state.get("fingerprint") is not None, "errored": bool(final and final.status == "ERROR"),
        "fields": fields, "lines": lines, "flag_groups": groups, "checklist": checklist,
        "open_questions": approval.open_questions if approval else [], "rounds": rounds,
    }

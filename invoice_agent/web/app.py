"""The review screen: a server-rendered FastAPI app over the run history (queue, invoice detail, human decision).
It sits at the end of the pipeline: a run paused in human_review is resumed here through runner.resume_review.
Everything shown is translated into plain language for a PE analyst/VP: dollars, risks and reasons, never raw JSON.
"""
import logging
import threading
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps
from invoice_agent.infra import db
from invoice_agent.infra.logging_setup import RUN_LOGGER
from invoice_agent.models import Flag, PipelineState, Uncertainty
from invoice_agent.runner import hydrate, resume_review
from invoice_agent.utils.guardrail import HUMAN_CONFIRMATION_REASON
from invoice_agent.web.highlight import highlight
from invoice_agent.web.security import ALLOWED_HOSTS, is_same_origin, needs_origin_check

log = logging.getLogger(RUN_LOGGER)

HERE = Path(__file__).parent

STATUS_LABELS = {
    "PAID": "Paid", "REJECTED": "Rejected", "NEEDS_HUMAN_REVIEW": "Needs your review",
    "DUPLICATE": "Duplicate", "ERROR": "Error",
}
DECIDED_BY_LABELS = {
    "agents": "the analyst and VP agents", "guardrail": "the automatic safety check",
    "human": "a reviewer", "system": "the system",
}
SEVERITY_GROUPS = [
    ("must_reject", "Blocking", "Any one of these stops payment."),
    ("needs_scrutiny", "Needs a look", "Allowed only with a clear explanation."),
    ("info", "FYI", "Context — not decisive on its own."),
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
    "line_total_exceeds_price": "Line total is more than qty × price", "total_unverifiable": "Total can't be verified",
    "invoice_number_reused": "Invoice number already billed", "price_mismatch": "Price differs from our records",
    "arithmetic_mismatch": "Numbers don't reconcile", "over_10k": "Over $10,000 — extra checks required",
    "due_date_missing": "Due date missing", "due_before_or_on_invoice_date": "Due on or before the invoice date",
    "non_usd_currency": "Not in US dollars", "missing_unit_price": "Unit price missing",
    "extraction_issue": "The reader was unsure", "urgency_language": "Payment-pressure language",
    "vendor_name_change": "Vendor name changed",
}

# Blocking reasons phrased for a sentence like "Blocked: 2 items aren't in inventory".
# A pair is (singular, plural) and gets a count; a plain string is a one-off fact.
_BLOCKING_PHRASES: dict[str, tuple[str, str] | str] = {
    "unknown_item": ("item isn't in inventory", "items aren't in inventory"),
    "zero_stock_item": ("item has zero stock", "items have zero stock"),
    "exceeds_stock": ("item is ordered beyond stock on hand", "items are ordered beyond stock on hand"),
    "non_positive_quantity": ("line has a zero or negative quantity", "lines have a zero or negative quantity"),
    "missing_quantity": ("line has no quantity", "lines have no quantity"),
    "line_total_exceeds_price": ("line is billed above qty × price", "lines are billed above qty × price"),
    "negative_total": "the total is negative",
    "missing_vendor": "no vendor is named",
    "missing_total": "no total is stated",
    "no_line_items": "there are no line items",
    "total_exceeds_line_items": "the total is more than the line items add up to",
    "total_unverifiable": "the total can't be verified",
    "invoice_number_reused": "this invoice number was already billed with different contents",
}

# Invoice header fields shown in the extracted-fields table, in reading order.
_FIELDS = [
    ("invoice_number", "Invoice number", "text"), ("vendor", "Vendor", "text"), ("date", "Invoice date", "date"),
    ("due_date", "Due date", "date"), ("payment_terms", "Payment terms", "text"), ("currency", "Currency", "text"),
    ("subtotal", "Subtotal", "money"), ("tax", "Tax", "money"), ("shipping", "Shipping", "money"),
    ("total", "Total", "money"), ("notes", "Notes", "text"),
]


# ---------- formatting ----------

def money(value, currency: str = "USD") -> str:
    """Format an amount as $1,234.56 (or "1,234.56 EUR" for other currencies); a dash when unknown."""
    if value is None or value == "":
        return "—"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    sign = "−" if d < 0 else ""
    body = f"{abs(d):,.2f}"
    return f"{sign}${body}" if currency.upper() == "USD" else f"{sign}{body} {currency.upper()}"


def quantity(value) -> str:
    """Format a quantity without trailing zeros (10, not 10.00)."""
    if value is None:
        return "—"
    return f"{Decimal(value).normalize():,f}"


def nice_date(value) -> str:
    """Format a date as 15 Jan 2026; a dash when unknown."""
    # value.day instead of %-d: the no-padding flag is not portable (it fails on Windows).
    return f"{value.day} {value.strftime('%b %Y')}" if isinstance(value, date) else "—"


def nice_time(iso: str | None) -> str:
    """Format a stored ISO timestamp as "17 Nov, 14:32 UTC" for the queue (times are stored in UTC)."""
    if not iso:
        return "—"
    t = datetime.fromisoformat(iso)
    return f"{t.day} {t.strftime('%b, %H:%M')} UTC"


def _plural(n: int, pair: tuple[str, str]) -> str:
    """'1 item isn't…' or '3 items aren't…'."""
    return f"{n} {pair[0] if n == 1 else pair[1]}"


def _join(parts: list[str]) -> str:
    """Join phrases as 'a', 'a and b' or 'a, b and c'."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def blocking_summary(flags: list[Flag]) -> str | None:
    """One plain sentence fragment for all must_reject flags (e.g. "2 items aren't in inventory"), or None."""
    counts: dict[str, int] = {}
    others: list[str] = []
    for f in flags:
        if f.severity != "must_reject":
            continue
        if f.code in _BLOCKING_PHRASES:
            counts[f.code] = counts.get(f.code, 0) + 1
        else:
            others.append(f.message)
    parts = []
    for code, n in counts.items():
        phrase = _BLOCKING_PHRASES[code]
        parts.append(_plural(n, phrase) if isinstance(phrase, tuple) else phrase)
    parts += others
    return _join(parts) if parts else None


def _first_sentence(text: str, width: int = 120) -> str:
    """The first sentence of a reasoning text on one line, cut to width."""
    line = " ".join((text or "").split())
    for stop in (". ", "; "):
        if stop in line:
            line = line.split(stop, 1)[0] + ("." if stop == ". " else "")
            break
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


# ---------- view models ----------

def verdict(state: PipelineState) -> str:
    """The detail page's top sentence: what happened to this invoice, in plain English."""
    final, flags, approval = state["final"], state.get("flags", []), state.get("approval")
    blocked = blocking_summary(flags)
    if final.status == "NEEDS_HUMAN_REVIEW":
        # The guardrail's hold overwrites approval.reasoning; recognise it to explain why a person is needed.
        if final.reasoning == HUMAN_CONFIRMATION_REASON:
            return "Agents approved, but prices or totals differ from our records — please confirm"
        rounds = len(approval.rounds) if approval else 0
        return f"Agents couldn't agree after {rounds} round{'s' if rounds != 1 else ''} — your call"
    if final.status == "REJECTED":
        if final.decided_by == "human":
            return "Rejected by a reviewer"
        return f"Rejected: {blocked}" if blocked else "Rejected by the agents"
    if final.status == "PAID":
        inv = state.get("invoice")
        who = "after a reviewer approved it" if final.decided_by == "human" else "— approved by the agents"
        to = f" to {inv.vendor}" if inv and inv.vendor else ""
        amount = money(inv.total, inv.currency) if inv else ""
        return f"Paid {amount}{to} {who}"
    if final.status == "DUPLICATE":
        return "Duplicate — this invoice was already processed, so it wasn't paid again"
    return "We couldn't process this file"


def why_line(state: PipelineState) -> str:
    """The queue's one-line "why" for a run."""
    final = state["final"]
    if final.status == "NEEDS_HUMAN_REVIEW":
        return verdict(state)
    if final.status == "REJECTED" and final.decided_by != "human":
        blocked = blocking_summary(state.get("flags", []))
        if blocked:
            return f"Blocked: {blocked}"
    if final.status == "DUPLICATE":
        return "Already processed — not paid twice"
    if final.status == "ERROR":
        # No raw text means the read step itself failed; otherwise a later stage (extraction, agents) did.
        prefix = "Processing failed: " if state.get("raw_text") else "Couldn't read this file: "
        return prefix + _first_sentence(final.reasoning, 80)
    if final.decided_by == "human":
        return ("Approved by a reviewer: " if final.status == "PAID" else "Rejected by a reviewer: ") + \
            _first_sentence(final.reasoning, 90)
    return _first_sentence(final.reasoning)


def _humanize_reason(u: Uncertainty) -> str:
    """Turn an uncertainty's reason into reviewer-friendly words."""
    if u.reason.startswith("ingestion agent confidence"):
        return "The reader wasn't fully sure of this value." if u.confidence == "medium" else \
            "The reader was unsure of this value."
    return u.reason[0].upper() + u.reason[1:] if u.reason else ""


def _amount_of(row: dict) -> Decimal:
    """A run row's total as a Decimal (0 when unknown) for the queue's dollar totals."""
    try:
        return Decimal(row["amount"]) if row.get("amount") else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def queue_view(rows: list[dict]) -> dict:
    """Rows and dollar totals for the queue page."""
    totals = {"PAID": Decimal(0), "REJECTED": Decimal(0), "NEEDS_HUMAN_REVIEW": Decimal(0)}
    review_count = 0
    items = []
    for row in rows:
        state = hydrate(row["state"])
        inv = state.get("invoice")
        currency = inv.currency if inv else "USD"
        status = row["status"] or "ERROR"
        # Dollar totals only add up USD invoices; a EUR amount summed as dollars would mislead.
        if status in totals and currency.upper() == "USD":
            totals[status] += _amount_of(row)
        review_count += status == "NEEDS_HUMAN_REVIEW"
        items.append({
            "run_id": row["run_id"], "status": status, "status_label": STATUS_LABELS.get(status, status),
            "number": row["invoice_number"] or Path(row["source_path"] or "").name or row["run_id"],
            "vendor": row["vendor"] or "—", "amount": money(row["amount"], currency),
            "why": why_line(state) if state.get("final") else "", "updated": nice_time(row["updated_at"]),
        })
    return {"rows": items, "paid": money(totals["PAID"]), "rejected": money(totals["REJECTED"]),
            "review_count": review_count, "review_amount": money(totals["NEEDS_HUMAN_REVIEW"])}


def _worst_severity(flags: list[Flag]) -> str | None:
    """The most serious severity among flags, for tinting a line item row."""
    for sev, _, _ in SEVERITY_GROUPS:
        if any(f.severity == sev for f in flags):
            return sev
    return None


def detail_view(row: dict) -> dict:
    """Everything the detail page shows for one run, already formatted for display."""
    state = hydrate(row["state"])
    final, inv, approval = state["final"], state.get("invoice"), state.get("approval")
    flags, uncs = state.get("flags", []), state.get("uncertainties", [])
    currency = inv.currency if inv else "USD"
    by_path = {u.field_path: u for u in uncs}

    def unc_view(path: str) -> dict | None:
        """The uncertainty on a field, formatted, or None if the reader was confident."""
        u = by_path.get(path)
        if u is None:
            return None
        return {"confidence": u.confidence, "label": CONFIDENCE_LABELS[u.confidence], "raw": u.raw,
                "reason": _humanize_reason(u)}

    fields = []
    for key, label, kind in _FIELDS:
        value = getattr(inv, key, None) if inv else None
        if key in ("shipping", "notes") and value is None and key not in by_path:
            continue  # optional fields most invoices don't have: hide rather than show a row of dashes
        shown = money(value, currency) if kind == "money" else nice_date(value) if kind == "date" else (value or "—")
        fields.append({"label": label, "value": shown, "money": kind == "money", "unc": unc_view(key)})

    lines = []
    for i, it in enumerate(inv.items if inv else []):
        line_flags = [f for f in flags if (f.field_path or "").startswith(f"items[{i}]")]
        lines.append({
            "name": it.name_raw, "sku": it.sku if it.sku and it.sku != it.name_raw else None,
            "suggestion": it.sku_suggestion, "qty": quantity(it.qty), "unit_price": money(it.unit_price, currency),
            "line_total": money(it.line_total, currency), "note": it.note, "severity": _worst_severity(line_flags),
            "unc": {k: unc_view(f"items[{i}].{k}") for k in ("name", "qty", "unit_price", "line_total")},
        })

    groups = []
    for sev, title, hint in SEVERITY_GROUPS:
        members = [{"title": FLAG_TITLES.get(f.code, f.code.replace("_", " ").capitalize()), "message": f.message}
                   for f in flags if f.severity == sev]
        if members:
            groups.append({"severity": sev, "title": title, "hint": hint, "flags": members})

    rounds, checklist = [], None
    for r in (approval.rounds if approval else []):
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
            checklist = [{"label": CHECKLIST_LABELS[k], "answer": (rec.checklist.get(k) or "").strip()}
                         for k in CHECKLIST_KEYS]

    blocking = [{"title": FLAG_TITLES.get(f.code, f.code), "message": f.message}
                for f in flags if f.severity == "must_reject"]
    raw_text = state.get("raw_text")
    return {
        "run_id": row["run_id"], "status": final.status, "status_label": STATUS_LABELS.get(final.status, final.status),
        "number": (inv.invoice_number if inv else None) or Path(state.get("source_path", "")).name,
        "vendor": (inv.vendor if inv else None) or "Unknown vendor",
        "amount": money(inv.total if inv else None, currency), "approve_amount": money(inv.total, currency) if inv else "",
        "file_name": Path(state.get("source_path", "")).name,
        "invoice_date": nice_date(inv.date) if inv else "—", "due_date": nice_date(inv.due_date) if inv else "—",
        "verdict": verdict(state), "reasoning": final.reasoning, "decided_by": DECIDED_BY_LABELS[final.decided_by],
        "human_note": final.human_note, "paid": bool(final.payment_result), "duplicate_of": state.get("duplicate_of"),
        "fields": fields, "lines": lines, "flag_groups": groups, "blocking": blocking,
        "blocking_summary": blocking_summary(flags), "uncertain_count": len(uncs),
        # Tooltips on the highlights get the same plain-language reasons as the fields table.
        "source": highlight(raw_text, [u.model_copy(update={"reason": _humanize_reason(u)}) for u in uncs])
        if raw_text else None,
        "checklist": checklist, "open_questions": approval.open_questions if approval else [], "rounds": rounds,
        "awaiting": final.status == "NEEDS_HUMAN_REVIEW",
    }


# ---------- app ----------

def create_app(deps: Deps, graph, conn) -> FastAPI:
    """Build the review-screen app over a runtime (real or fake deps/graph) and the app database connection."""
    app = FastAPI(title="Invoice review", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    # Answer only to local host names, so a hostile site can't reach this server via DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    # One decision at a time: two quick clicks must never resume (and pay) the same run twice.
    decision_lock = threading.Lock()

    def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
        """Render a template with the request in context."""
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def render_detail(request: Request, row: dict, status_code: int = 200, **extra) -> HTMLResponse:
        """Render the detail page for a run, with optional form error, notice and kept form values."""
        context = {"inv": detail_view(row), "error": None, "notice": None, "form": {}, **extra}
        return render(request, "detail.html", context, status_code)

    def not_found(request: Request) -> HTMLResponse:
        """A friendly 404 page for an unknown run id."""
        return render(request, "queue.html", {"missing": True, **queue_view(db.list_runs(conn))}, 404)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Refuse any state-changing request (decisions, uploads) that doesn't come from this screen's own pages."""
        if needs_origin_check(request) and not is_same_origin(request):
            return render(request, "message.html", {
                "title": "Request blocked",
                "message": "This action has to come from the review screen itself, so it was blocked. "
                           "Nothing was approved, rejected or paid. Open the invoice here and try again.",
            }, 403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request):
        """The invoice queue: dollar totals, then every run with those awaiting review first."""
        return render(request, "queue.html", {"missing": False, **queue_view(db.list_runs(conn))})

    @app.get("/invoices/{run_id}", response_class=HTMLResponse)
    def detail(request: Request, run_id: str, decided: int = 0):
        """One invoice: verdict, source document, extracted fields, flags, agent reasoning, decision panel."""
        row = db.get_run(conn, run_id)
        if row is None:
            return not_found(request)
        return render_detail(request, row, notice="Your decision has been recorded." if decided else None)

    @app.post("/invoices/{run_id}/decision", response_class=HTMLResponse)
    def decide(request: Request, run_id: str, decision: str = Form(""), note: str = Form(""),
               justification: str = Form("")):
        """Apply a reviewer's Approve/Reject to a paused run, then redirect back to its detail page."""
        form = {"note": note, "justification": justification}
        with decision_lock:
            row = db.get_run(conn, run_id)
            if row is None:
                return not_found(request)
            if row["status"] != "NEEDS_HUMAN_REVIEW":
                label = STATUS_LABELS.get(row["status"], row["status"]).lower()
                return render_detail(request, row, 409,
                                     notice=f"This invoice isn't waiting for a decision — it's already {label}.")
            if decision not in ("APPROVE", "REJECT"):
                return render_detail(request, row, 400, error="Choose Approve or Reject.", form=form)
            has_blocking = any(f.get("severity") == "must_reject" for f in row["state"].get("flags", []))
            message = ("A justification is required to approve over blocking issues — "
                       "write why paying is right despite them. It goes on the audit record.")
            if decision == "APPROVE" and has_blocking and not justification.strip():
                return render_detail(request, row, 400, error=message, form=form)
            try:
                resume_review(run_id, decision, note.strip(), justification.strip() or None, deps, graph)
            except ValueError as exc:
                # Defence in depth: the graph's own resume schema also refuses an unjustified approval.
                if "justification required" not in str(exc):
                    raise
                return render_detail(request, row, 400, error=message, form=form)
            except Exception:
                # E.g. a missing or corrupt checkpoint: log the detail, show the reviewer a plain notice.
                log.exception("decision failed", extra={"fields": {"run_id": run_id}})
                return render_detail(request, db.get_run(conn, run_id) or row, 500, form=form, notice=(
                    "We couldn't record that decision — the run's saved progress is missing or damaged. "
                    "Nothing was paid."))
        return RedirectResponse(f"/invoices/{run_id}?decided=1", status_code=303)

    return app

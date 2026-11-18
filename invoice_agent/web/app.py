"""The review screen: a server-rendered FastAPI app over the run history (queue, invoice detail, human decision).
It sits at the end of the pipeline: a run paused in human_review is resumed here through runner.resume_review.
It is also an intake: uploaded files (or the whole sample set) run through the pipeline in background threads,
and the detail page shows each run's stage live. Everything shown is translated into plain language for a PE analyst/VP: dollars, risks and reasons, never raw JSON.
"""
import hashlib
import logging
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_agent.agents.approval.policy import CHECKLIST_KEYS
from invoice_agent.graph import Deps
from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
from invoice_agent.infra.logging_setup import RUN_LOGGER
from invoice_agent.models import FinalOutcome, Flag, PipelineState, Uncertainty
from invoice_agent.runner import build_runtime, hydrate, invoice_files, keeps_result, process_file, resume_review
from invoice_agent.utils.guardrail import HUMAN_CONFIRMATION_REASON
from invoice_agent.utils.reader import SUPPORTED_SUFFIXES
from invoice_agent.web.highlight import highlight
from invoice_agent.web.security import ALLOWED_HOSTS, is_same_origin, needs_origin_check

log = logging.getLogger(RUN_LOGGER)

HERE = Path(__file__).parent
# The sample invoices, found from this file (not the current directory) so the button works wherever the server starts.
SAMPLES_DIR = HERE.parent.parent / "data" / "invoices"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Room for the multipart envelope (boundaries, headers) around a file of the maximum size.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_STEM_CHARS = 100
ALLOWED_TYPES_TEXT = "PDF, TXT, JSON, CSV or XML"
TOO_LARGE_MESSAGE = "Invoices can be up to 5 MB. This one is larger, so it wasn't processed."
INTERRUPTED_REASON = "Processing was interrupted — re-upload the file to try again."

STATUS_LABELS = {
    "PAID": "Paid", "REJECTED": "Rejected", "NEEDS_HUMAN_REVIEW": "Needs your review",
    "DUPLICATE": "Duplicate", "ERROR": "Error", "PROCESSING": "Processing",
}

# The live tracker's steps, in plain language. A run's stored stage is the graph node that last finished.
PROGRESS_STEPS = ["Reading file", "Extracting details (ingestion agent)", "Checking against inventory",
                  "Analyst ⇄ VP review", "Decision"]
# Finished node -> index of the step now in progress. Every node after approval (or a failure) is the decision.
_STEP_AFTER_NODE = {"read": 1, "extract": 2, "validate": 3, "approval": 4, "mark_duplicate": 4, "guardrail": 4,
                    "human_review": 4, "pay": 4, "log_rejection": 4, "fail": 4}
# Stage set on files of a sample batch that haven't started yet.
QUEUED = "queued"
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


def current_step(stage: str | None) -> int:
    """Index of the tracker step in progress for a stored stage; -1 while waiting its turn in a batch."""
    if stage == QUEUED:
        return -1
    # No stage yet (or "started") means the job has just begun reading the file.
    return _STEP_AFTER_NODE.get(stage or "", 0)


def stage_label(stage: str | None) -> str:
    """The plain-language name of what a PROCESSING run is doing now."""
    step = current_step(stage)
    return "Waiting its turn" if step < 0 else PROGRESS_STEPS[step]


def _declared_length(request: Request) -> int:
    """The request's Content-Length header as a number (0 when missing or malformed)."""
    try:
        return int(request.headers.get("content-length", "0"))
    except ValueError:
        return 0


def display_name(row: dict) -> str:
    """The file's name as the user uploaded it (without the run-id prefix uploads are saved with)."""
    return Path(row["source_path"] or "").name.removeprefix(f"{row['run_id']}_")


def safe_filename(name: str) -> str:
    """Reduce an uploaded file name to [A-Za-z0-9._-] with a stem of at most 100 characters, dropping any folders
    so it can't point outside uploads/; "" when nothing usable is left (e.g. ".txt" or "...")."""
    # Both separators: a Windows browser may send C:\path\file.txt.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot:
        stem, suffix = cleaned, ""
    # Leading dots would make a hidden file; an empty stem ("x/.txt") means there's no real name at all.
    stem = stem.lstrip(".")
    if not stem:
        return ""
    # Cap the stem so <run_id>_<name> stays well under the file system's 255-character limit.
    return stem[:MAX_STEM_CHARS] + (f".{suffix}" if dot else "")


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
        processing = status == "PROCESSING"
        # Dollar totals only add up USD invoices; a EUR amount summed as dollars would mislead.
        if status in totals and currency.upper() == "USD":
            totals[status] += _amount_of(row)
        review_count += status == "NEEDS_HUMAN_REVIEW"
        items.append({
            "run_id": row["run_id"], "status": status, "status_label": STATUS_LABELS.get(status, status),
            "number": row["invoice_number"] or display_name(row) or row["run_id"],
            "vendor": row["vendor"] or "—", "amount": money(row["amount"], currency),
            # A run in progress may still carry its previous result's state, so its "why" is the live stage.
            "why": stage_label(row.get("stage")) if processing else why_line(state) if state.get("final") else "",
            "updated": nice_time(row["updated_at"]), "processing": processing,
        })
    return {"rows": items, "paid": money(totals["PAID"]), "rejected": money(totals["REJECTED"]),
            "review_count": review_count, "review_amount": money(totals["NEEDS_HUMAN_REVIEW"])}


def progress_view(row: dict) -> dict:
    """The tracker page for a run still PROCESSING: file name and each step as done, current or still to come."""
    step = current_step(row.get("stage"))
    steps = [{"label": label, "state": "done" if i < step else "current" if i == step else "todo"}
             for i, label in enumerate(PROGRESS_STEPS)]
    return {"run_id": row["run_id"], "file_name": display_name(row), "steps": steps,
            "label": stage_label(row.get("stage")), "started": nice_time(row["updated_at"])}


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
    file_name = display_name({"run_id": row["run_id"], "source_path": state.get("source_path")})
    return {
        "run_id": row["run_id"], "status": final.status, "status_label": STATUS_LABELS.get(final.status, final.status),
        "number": (inv.invoice_number if inv else None) or file_name,
        "vendor": (inv.vendor if inv else None) or "Unknown vendor",
        "amount": money(inv.total if inv else None, currency), "approve_amount": money(inv.total, currency) if inv else "",
        "file_name": file_name,
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


# ---------- background processing ----------

def _save_error(conn: sqlite3.Connection, run_id: str, source_path: str | None, reason: str,
                content_hash: str | None = None) -> None:
    """End a run as ERROR with a plain reason (it never reached, or never finished, the pipeline)."""
    state: PipelineState = {"run_id": run_id, "source_path": source_path or "",
                            "final": FinalOutcome(status="ERROR", decided_by="system", reasoning=reason)}
    # Keeping the hash lets a re-upload of the same file reuse this run instead of adding another row.
    if content_hash:
        state["content_hash"] = content_hash
    db.save_run(conn, state)


def _close_runtime(deps: Deps, graph) -> None:
    """Close a background job's own database connections (run history and checkpoints)."""
    deps.conn.close()
    checkpoint_conn = getattr(getattr(graph, "checkpointer", None), "conn", None)
    if isinstance(checkpoint_conn, sqlite3.Connection):
        checkpoint_conn.close()


def run_jobs(jobs: list[tuple[str, Path, str]], runtime_factory: Callable[[], tuple[Deps, object]],
             app_db: Path) -> None:
    """Background job: run each (run_id, file, content hash) through the pipeline in order, on this thread's own runtime."""
    try:
        # Own runtime per job: a SQLite connection must never be shared between threads.
        deps, graph = runtime_factory()
    except Exception as exc:
        # E.g. no API key: the runs can't start, so say why instead of leaving them PROCESSING forever.
        log.warning("background job could not start", extra={"fields": {"error": str(exc)}})
        conn = db.connect(app_db)
        try:
            for run_id, path, digest in jobs:
                _save_error(conn, run_id, str(path), f"Couldn't start processing: {exc}", digest)
        finally:
            conn.close()
        return
    try:
        for run_id, path, digest in jobs:
            try:
                db.set_stage(deps.conn, run_id, "started")
                state = process_file(path, deps, graph, run_id=run_id,
                                     on_stage=lambda node, rid=run_id: db.set_stage(deps.conn, rid, node))
                if state["run_id"] != run_id:
                    # Two uploads of the same file raced; the other run holds the result.
                    _save_error(deps.conn, run_id, str(path), "This file matches another invoice that was "
                                "being processed at the same time — open that one from the queue.", digest)
            except Exception as exc:
                # process_file never raises for a bad file, so this is a database problem; carry on with the batch.
                log.exception("background run failed", extra={"fields": {"run_id": run_id}})
                _save_error(deps.conn, run_id, str(path), f"Processing failed: {exc}", digest)
    finally:
        _close_runtime(deps, graph)


# ---------- app ----------

def create_app(deps: Deps, graph, conn, settings: Settings | None = None,
               runtime_factory: Callable[[], tuple[Deps, object]] | None = None) -> FastAPI:
    """Build the review-screen app over a runtime (real or fake deps/graph) and the app database connection.
    settings: where uploads and the app db live; runtime_factory: builds a fresh runtime for each background job."""
    settings = settings or Settings()
    runtime_factory = runtime_factory or (lambda: build_runtime(settings))
    uploads = settings.var_dir / "uploads"
    app = FastAPI(title="Invoice review", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    # Answer only to local host names, so a hostile site can't reach this server via DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    # One decision at a time: two quick clicks must never resume (and pay) the same run twice.
    decision_lock = threading.Lock()
    # One intake at a time, so two uploads of the same file can't both claim it as new.
    intake_lock = threading.Lock()
    # Background job threads still running (finished ones are pruned), so tests can wait for them to finish.
    app.state.jobs = []

    # A run still PROCESSING at startup belonged to a server that was stopped mid-run: nothing will finish it.
    for row in db.list_runs(conn):
        if row["status"] == "PROCESSING":
            _save_error(conn, row["run_id"], row["source_path"], INTERRUPTED_REASON, row["content_hash"])

    def start_jobs(jobs: list[tuple[str, Path, str]]) -> None:
        """Process (run_id, file, content hash) jobs one after another on a new daemon thread."""
        thread = threading.Thread(target=run_jobs, args=(jobs, runtime_factory, settings.app_db), daemon=True)
        # Drop finished threads so a long-running server doesn't keep one per upload forever.
        app.state.jobs = [t for t in app.state.jobs if t.is_alive()] + [thread]
        thread.start()

    def claim(content_hash: str) -> tuple[str, bool]:
        """The run id for a file's contents and whether it needs processing (False: open the existing run instead)."""
        existing = db.find_run_by_content_hash(conn, content_hash)
        # Same re-run policy as the CLI: never re-run a paid, paused or human-decided invoice, or one in progress.
        if existing and (existing["status"] == "PROCESSING" or keeps_result(existing)):
            return existing["run_id"], False
        return (existing["run_id"] if existing else uuid.uuid4().hex[:12]), True

    def refuse(request: Request, status_code: int, title: str, message: str) -> HTMLResponse:
        """A plain page explaining why an upload was refused."""
        return render(request, "message.html", {"title": title, "message": message}, status_code)

    def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
        """Render a template with the request in context."""
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def render_detail(request: Request, row: dict, status_code: int = 200, **extra) -> HTMLResponse:
        """Render the detail page for a run, with optional form error, notice and kept form values."""
        if row["status"] == "PROCESSING":
            # No result yet: show the live stage tracker instead.
            return render(request, "processing.html", {"run": progress_view(row), "notice": extra.get("notice")},
                          status_code)
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
        too_big = _declared_length(request) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
        if request.url.path == "/upload" and too_big:
            # Refuse before the body is parsed; the route checks the real size again as a backstop.
            return refuse(request, 413, "File too large", TOO_LARGE_MESSAGE)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request):
        """The invoice queue: dollar totals, then every run with those awaiting review first."""
        return render(request, "queue.html", {"missing": False, **queue_view(db.list_runs(conn))})

    @app.get("/invoices/{run_id}", response_class=HTMLResponse)
    def detail(request: Request, run_id: str, decided: int = 0, seen: int = 0):
        """One invoice: verdict, source document, extracted fields, flags, agent reasoning, decision panel."""
        row = db.get_run(conn, run_id)
        if row is None:
            return not_found(request)
        notice = None
        if decided:
            notice = "Your decision has been recorded."
        elif seen and row["status"] == "PROCESSING":
            notice = "This file is already being processed — here is its progress."
        elif seen:
            notice = "This file was uploaded before, so here is that invoice — it wasn't processed or paid again."
        return render_detail(request, row, notice=notice)

    @app.get("/invoices/{run_id}/status")
    def status(run_id: str):
        """Where a run is now, for live.js: status, last finished stage and, while processing, the step in progress."""
        row = db.get_run(conn, run_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        processing = row["status"] == "PROCESSING"
        return {"status": row["status"], "stage": row["stage"],
                "step": current_step(row["stage"]) if processing else None,
                "label": stage_label(row["stage"]) if processing else STATUS_LABELS.get(row["status"])}

    @app.post("/upload", response_class=HTMLResponse)
    def upload(request: Request, file: UploadFile | None = File(None)):
        """Save an uploaded invoice, mark it PROCESSING, start it in the background and open its live page."""
        if file is None or not file.filename:
            return refuse(request, 400, "No file chosen", "Choose an invoice file to upload, then try again.")
        name = safe_filename(file.filename)
        if not name:
            return refuse(request, 400, "File name not allowed",
                          f"“{file.filename}” has no usable name. Rename the file (for example invoice.pdf) and try again.")
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            return refuse(request, 400, "File type not supported",
                          f"Upload a {ALLOWED_TYPES_TEXT} file. “{file.filename}” isn't one of those, "
                          "so it wasn't processed.")
        # Read one byte past the limit: enough to know it's too big without holding a huge file in memory.
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            return refuse(request, 413, "File too large", TOO_LARGE_MESSAGE)
        digest = hashlib.sha256(content).hexdigest()  # the same content hash the reader computes
        with intake_lock:
            run_id, needed = claim(digest)
            if not needed:
                return RedirectResponse(f"/invoices/{run_id}?seen=1", status_code=303)
            dest = uploads / f"{run_id}_{name}"
            # Defence in depth: the sanitized name can't contain a folder, but never write outside uploads/.
            if dest.resolve().parent != uploads.resolve():
                return refuse(request, 400, "File name not allowed", "Rename the file and try again.")
            uploads.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            db.create_pending_run(conn, run_id, str(dest), digest)
        start_jobs([(run_id, dest, digest)])
        return RedirectResponse(f"/invoices/{run_id}", status_code=303)

    @app.post("/process-samples")
    def process_samples():
        """Queue every sample invoice not already settled and process them one by one in the background."""
        jobs = []
        with intake_lock:
            for path in invoice_files(SAMPLES_DIR):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                run_id, needed = claim(digest)
                if not needed:
                    continue
                db.create_pending_run(conn, run_id, str(path), digest)
                # Shown as "Waiting its turn" until the job reaches this file.
                db.set_stage(conn, run_id, QUEUED)
                jobs.append((run_id, path, digest))
        if jobs:
            start_jobs(jobs)
        return RedirectResponse("/", status_code=303)

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

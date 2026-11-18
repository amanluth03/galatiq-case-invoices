"""The review screen at the end of the pipeline (read -> extract -> validate -> analyst/VP approval -> guardrail ->
pay / reject / waiting for you). The landing page (/) says what the agent does; the dashboard (/dashboard) has the
drop card and every invoice; each invoice has its own page (/invoices/<id>) telling its story live, step by step, with
the full invoice details under it. Files run in background threads, and a run paused for a person is approved or
rejected on its page (via runner.resume_review), under a review brief saying why it needs them and what to check.
Everything shown is plain language for a PE analyst/VP: dollars, risks and reasons, never codes or raw JSON.
"""
import hashlib
import logging
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_agent.graph import Deps
from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
from invoice_agent.infra.logging_setup import RUN_LOGGER
from invoice_agent.models import FinalOutcome, PipelineState, Round
from invoice_agent.runner import (
    _paid_checkpoint, build_runtime, hydrate, invoice_files, keeps_result, process_file, resume_review, run_batch,
)
from invoice_agent.utils.reader import SUPPORTED_SUFFIXES
from invoice_agent.web.security import ALLOWED_HOSTS, is_same_origin, needs_origin_check
from invoice_agent.web.story import (
    NOT_STARTED, QUEUED, STATUS_LABELS, blocking_summary, build_story, display_name, lookup_text, money,
    plain_message, story_json,
)
from invoice_agent.web.views import brief_view, dashboard_view, details_view, sort_rows

log = logging.getLogger(RUN_LOGGER)

HERE = Path(__file__).parent
# The sample invoices, found from this file (not the current directory) so the button works wherever the server starts.
SAMPLES_DIR = HERE.parent.parent / "data" / "invoices"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Earlier addresses of the dashboard (it used to be the home page), kept working by redirecting to /dashboard.
OLD_DASHBOARD_URLS = ("/home", "/invoices")
# Room for the multipart envelope (boundaries, headers) around a file of the maximum size.
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_STEM_CHARS = 100
ALLOWED_TYPES_TEXT = "PDF, TXT, JSON, CSV or XML"
TOO_LARGE_MESSAGE = "Invoices can be up to 5 MB. This one is larger, so it wasn't processed."
INTERRUPTED_REASON = "Processing was interrupted: re-upload the file to try again."
# How many tool lookups a running agent shows at once (the latest ones).
MAX_LOOKUPS = 6
STATIC_DIR = HERE / "static"


def static_versions(directory: Path = STATIC_DIR) -> dict[str, str]:
    """A short hash of each static file's contents, computed once at startup.
    Added to every static URL (?v=<hash>), so a browser never pairs a new page with a stale cached stylesheet."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:10] for p in directory.iterdir() if p.is_file()}


def _declared_length(request: Request) -> int:
    """The request's Content-Length header as a number (0 when missing or malformed)."""
    try:
        return int(request.headers.get("content-length", "0"))
    except ValueError:
        return 0


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

def row_state(row: dict) -> PipelineState:
    """A run row's stored state as typed models, including the live analyst/VP rounds of a running run."""
    state = hydrate(row["state"])
    if state.get("live_rounds"):
        state["live_rounds"] = [Round.model_validate(r) for r in state["live_rounds"]]
    return state


def row_status(row: dict) -> str:
    """A run row's status for the screen (ERROR when a row somehow has none)."""
    return row["status"] or "ERROR"


def run_story(conn: sqlite3.Connection, row: dict):
    """Build the story of one run row (live while PROCESSING), looking up the original's file name for a duplicate."""
    state = row_state(row)
    live = row_status(row) == "PROCESSING"
    original = None
    if state.get("duplicate_of"):
        other = db.get_run(conn, state["duplicate_of"])
        original = display_name(other["run_id"], other["source_path"]) if other else None
    return build_story(state, row.get("stage"), live, original_name=original, decided_at=row.get("updated_at"),
                       now=datetime.now(timezone.utc))


def running_title(steps) -> str | None:
    """The title of the step a live run is on (e.g. "Analyst agent is investigating..."), or None."""
    return next((s.title for s in steps if s.state == "running"), None)


def status_label(row: dict) -> str:
    """The pill text for a run: Paid, Rejected, Waiting for you, Duplicate, Error, Running or Queued."""
    if row_status(row) == "PROCESSING" and row.get("stage") == QUEUED:
        return "Queued"
    return STATUS_LABELS.get(row_status(row), row_status(row))


def list_view(rows: list[dict], selected: str | None) -> list[dict]:
    """The compact "Invoices" sidebar on an invoice page: file name, status pill and amount, waiting ones first."""
    items = []
    for row in sort_rows(rows):
        inv = row_state(row).get("invoice")
        items.append({
            "run_id": row["run_id"], "name": display_name(row["run_id"], row["source_path"]) or row["run_id"],
            "status": row_status(row).lower(), "label": status_label(row),
            "amount": money(row["amount"], inv.currency if inv else "USD") if row["amount"] else "",
            "selected": row["run_id"] == selected,
        })
    return items


def story_view(conn: sqlite3.Connection, row: dict) -> dict:
    """Everything an invoice page shows: header, the 7 steps, the decision panel's facts (with the review brief when
    it waits for a person) and the invoice details."""
    state = row_state(row)
    live = row_status(row) == "PROCESSING"
    if live and row.get("stage") in NOT_STARTED:
        # A re-run's row still holds the previous result until the first step finishes: show none of it.
        state = {"run_id": row["run_id"]}
    inv, flags = state.get("invoice"), state.get("flags") or []
    awaiting = row_status(row) == "NEEDS_HUMAN_REVIEW"
    brief = brief_view(state) if awaiting else None
    # The brief's snippets are marked in the original document so "Show in invoice" can jump to them.
    anchors = [c["snippet"] for c in brief["checks"] if c["jump"] and c["jump"]["anchor"]] if brief else []
    return {
        "run_id": row["run_id"], "live": live, "status": row_status(row).lower(), "label": status_label(row),
        "file_name": display_name(row["run_id"], row["source_path"]) or row["run_id"],
        "steps": run_story(conn, row),
        "details": details_view(state, anchors),
        "awaiting": awaiting, "brief": brief,
        "vendor": (inv.vendor if inv else None) or "the vendor",
        "approve_amount": money(inv.total, inv.currency) if inv else "",
        "blocking": [plain_message(f.message) for f in flags if f.severity == "must_reject"],
        "blocking_summary": blocking_summary(flags),
    }


def home_view(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """The dashboard of every run: summary tiles and one table row each."""
    states = {r["run_id"]: row_state(r) for r in rows}
    running = {r["run_id"]: running_title(run_story(conn, r)) for r in rows if row_status(r) == "PROCESSING"}
    return dashboard_view(rows, states, running)


# ---------- background processing ----------

def _save_error(conn: sqlite3.Connection, run_id: str, source_path: str | None, reason: str,
                content_hash: str | None = None) -> PipelineState:
    """End a run as ERROR with a plain reason (it never reached, or never finished, the pipeline); returns that state."""
    state: PipelineState = {"run_id": run_id, "source_path": source_path or "",
                            "final": FinalOutcome(status="ERROR", decided_by="system", reasoning=reason)}
    # Keeping the hash lets a re-upload of the same file reuse this run instead of adding another row.
    if content_hash:
        state["content_hash"] = content_hash
    db.save_run(conn, state)
    return state


def _tool_lookups(update: dict) -> list[str]:
    """Plain sentences for the tool calls in an agent's model step (e.g. "Looked up WidgetA stock and price")."""
    out = []
    for message in update.get("messages") or []:
        for call in getattr(message, "tool_calls", None) or []:
            text = lookup_text(call.get("name", ""), call.get("args") or {})
            if text:
                out.append(text)
    return out


def progress_recorder(conn: sqlite3.Connection, run_id: str) -> Callable[[tuple, str, dict], None]:
    """An on_update callback for process_file that keeps this run's partial state in its row as each node finishes,
    so the story (and its analyst/VP rounds and tool lookups) can be shown live."""
    # Built only from this run's own updates, so a re-run never shows the previous result's details.
    partial: dict = {"run_id": run_id}

    def on_update(namespace: tuple, node: str, update: dict) -> None:
        """Merge one node's output into the partial state and save it."""
        if not namespace:
            # A top-level pipeline node (read, extract, validate, approval, ...): its update is part of the state.
            partial.update(update)
            partial.pop("live_lookups", None)
            db.save_progress(conn, run_id, node, partial)
        elif len(namespace) == 1 and node in ("analyst", "vp"):
            # Inside the approval loop: the rounds so far (each analyst recommendation, then the VP's review).
            partial["live_rounds"] = update.get("rounds") or []
            partial["live_lookups"] = []
            # The next agent starts now (VP after the analyst, analyst after a send-back): restart its clock.
            db.save_progress(conn, run_id, None, partial, new_step=True)
        elif len(namespace) > 1:
            # Inside an agent's own tool loop: show what it is looking up.
            lookups = _tool_lookups(update)
            if lookups:
                partial["live_lookups"] = [*(partial.get("live_lookups") or []), *lookups][-MAX_LOOKUPS:]
                db.save_progress(conn, run_id, None, partial)

    return on_update


def _run_job(job: tuple[str, Path, str], deps: Deps, graph) -> PipelineState:
    """Process one (run_id, file, content hash) job on a worker's runtime, saving its progress for the live story."""
    run_id, path, digest = job
    try:
        db.set_stage(deps.conn, run_id, "started")
        state = process_file(path, deps, graph, run_id=run_id, on_update=progress_recorder(deps.conn, run_id))
        if state["run_id"] != run_id:
            # Two uploads of the same file raced; the other run holds the result.
            return _save_error(deps.conn, run_id, str(path), "This file matches another invoice that was "
                               "being processed at the same time: open that one from the Invoices list.", digest)
        return state
    except Exception as exc:
        # process_file never raises for a bad file, so this is a database problem; carry on with the batch.
        log.exception("background run failed", extra={"fields": {"run_id": run_id}})
        return _save_error(deps.conn, run_id, str(path), f"Processing failed: {exc}", digest)


def run_jobs(jobs: list[tuple[str, Path, str]], runtime_factory: Callable[[], tuple[Deps, object]],
             app_db: Path, workers: int = 1) -> None:
    """Background job: run each (run_id, file, content hash) through the pipeline, `workers` at a time.
    Same parallel path as the CLI batch: every worker has its own runtime, and Grok timeouts are retried once."""
    try:
        run_batch(jobs, _run_job, runtime_factory, workers)
    except Exception as exc:
        # E.g. no API key: the runtime can't be built, so say why instead of leaving runs PROCESSING forever.
        log.warning("background job could not start", extra={"fields": {"error": str(exc)}})
        conn = db.connect(app_db)
        try:
            for run_id, path, digest in jobs:
                row = db.get_run(conn, run_id)
                if row is None or row["status"] == "PROCESSING":
                    _save_error(conn, run_id, str(path), f"Couldn't start processing: {exc}", digest)
        finally:
            conn.close()


# ---------- app ----------

def create_app(deps: Deps, graph, conn, settings: Settings | None = None,
               runtime_factory: Callable[[], tuple[Deps, object]] | None = None) -> FastAPI:
    """Build the review-screen app over a runtime (real or fake deps/graph) and the app database connection.
    settings: where uploads and the app db live; runtime_factory: builds a fresh runtime for each background job.
    conn is used only at startup and (as deps.conn) for decisions under the lock; each request opens its own."""
    settings = settings or Settings()
    # The file behind conn: routes run on a threadpool, and one sqlite3 connection shared across threads is unsafe.
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    runtime_factory = runtime_factory or (lambda: build_runtime(settings))
    uploads = settings.var_dir / "uploads"
    app = FastAPI(title="Invoice review", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    versions = static_versions()

    def static_url(name: str) -> str:
        """A static file's URL with its content hash, so every change to the file is fetched fresh."""
        return f"/static/{name}?v={versions.get(name, '0')}"

    templates.env.globals["static_url"] = static_url
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

    def request_conn():
        """FastAPI dependency: a fresh connection to the app database for this request, closed when it ends."""
        c = db.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    def start_jobs(jobs: list[tuple[str, Path, str]], workers: int = 1) -> None:
        """Process (run_id, file, content hash) jobs, `workers` at a time, from a new daemon thread."""
        thread = threading.Thread(target=run_jobs, args=(jobs, runtime_factory, settings.app_db, workers), daemon=True)
        # Drop finished threads so a long-running server doesn't keep one per upload forever.
        app.state.jobs = [t for t in app.state.jobs if t.is_alive()] + [thread]
        thread.start()

    def claim(conn: sqlite3.Connection, content_hash: str) -> tuple[str, bool]:
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

    def render_home(request: Request, conn: sqlite3.Connection, status_code: int = 200, **extra) -> HTMLResponse:
        """The dashboard page: the drop card and the dashboard of every invoice."""
        rows = db.list_runs(conn)
        context = {"board": home_view(conn, rows), "sample_count": len(invoice_files(SAMPLES_DIR)),
                   "missing": False, **extra}
        return render(request, "home.html", context, status_code)

    def render_page(request: Request, conn: sqlite3.Connection, row: dict, status_code: int = 200,
                    **extra) -> HTMLResponse:
        """An invoice page: the story of `row`, its details, and the Invoices list beside it."""
        context = {"story": story_view(conn, row), "runs": list_view(db.list_runs(conn), row["run_id"]),
                   "error": None, "notice": None, "form": {}, **extra}
        return render(request, "invoice.html", context, status_code)

    def not_found(request: Request, conn: sqlite3.Connection) -> HTMLResponse:
        """A friendly 404: the dashboard with a notice that the invoice wasn't found."""
        return render_home(request, conn, 404, missing=True)

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
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # Always revalidate: a browser must never keep using an old stylesheet with a newer page.
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        """The landing page: what the agent does, and the way in (process an invoice, or the dashboard)."""
        return render(request, "landing.html", {})

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, conn: sqlite3.Connection = Depends(request_conn)):
        """The drop card and the dashboard of every invoice (waiting for you first, then running, then newest)."""
        return render_home(request, conn)

    for old in OLD_DASHBOARD_URLS:
        @app.get(old, include_in_schema=False)
        def old_dashboard():
            """An old dashboard address: send the browser to /dashboard."""
            return RedirectResponse("/dashboard", status_code=301)

    @app.get("/invoices/{run_id}", response_class=HTMLResponse)
    def detail(request: Request, run_id: str, decided: int = 0, seen: int = 0,
               conn: sqlite3.Connection = Depends(request_conn)):
        """One invoice's page: its story (live while it runs) and full details."""
        row = db.get_run(conn, run_id)
        if row is None:
            return not_found(request, conn)
        notice = None
        if decided:
            notice = "Your decision has been recorded."
        elif seen and row["status"] == "PROCESSING":
            notice = "This file is already being processed: here is its progress."
        elif seen:
            notice = "This file was uploaded before, so here is that invoice: it wasn't processed or paid again."
        return render_page(request, conn, row, notice=notice)

    @app.get("/invoices/{run_id}/status")
    def status(run_id: str, conn: sqlite3.Connection = Depends(request_conn)):
        """The run's story as JSON for live.js: status, pill label, whether it has finished, and the 7 steps."""
        row = db.get_run(conn, run_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"status": row_status(row), "stage": row["stage"], "label": status_label(row),
                "finished": row_status(row) != "PROCESSING",
                "file_name": display_name(row["run_id"], row["source_path"]),
                "steps": story_json(run_story(conn, row))}

    @app.post("/upload", response_class=HTMLResponse)
    def upload(request: Request, file: UploadFile | None = File(None),
               conn: sqlite3.Connection = Depends(request_conn)):
        """Save an uploaded invoice, mark it PROCESSING, start it in the background and open its live story."""
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
            run_id, needed = claim(conn, digest)
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
    def process_samples(conn: sqlite3.Connection = Depends(request_conn)):
        """Queue every sample invoice not already settled and process them in the background, several at a time."""
        jobs = []
        with intake_lock:
            for path in invoice_files(SAMPLES_DIR):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                run_id, needed = claim(conn, digest)
                if not needed:
                    continue
                db.create_pending_run(conn, run_id, str(path), digest)
                # Shown as "Queued" until the job reaches this file.
                db.set_stage(conn, run_id, QUEUED)
                jobs.append((run_id, path, digest))
        if jobs:
            start_jobs(jobs, settings.batch_workers)
        return RedirectResponse("/dashboard", status_code=303)

    @app.post("/invoices/{run_id}/decision", response_class=HTMLResponse)
    def decide(request: Request, run_id: str, decision: str = Form(""), note: str = Form(""),
               justification: str = Form(""), conn: sqlite3.Connection = Depends(request_conn)):
        """Apply a reviewer's Approve/Reject to a paused run, then redirect back to its story."""
        form = {"note": note, "justification": justification}
        with decision_lock:
            row = db.get_run(conn, run_id)
            if row is None:
                return not_found(request, conn)
            if row["status"] != "NEEDS_HUMAN_REVIEW":
                label = STATUS_LABELS.get(row["status"], row["status"]).lower()
                return render_page(request, conn, row, 409,
                                     notice=f"This invoice isn't waiting for a decision: it's already {label}.")
            if decision not in ("APPROVE", "REJECT"):
                return render_page(request, conn, row, 400, error="Choose Approve or Reject.", form=form)
            has_blocking = any(f.get("severity") == "must_reject" for f in row["state"].get("flags", []))
            message = ("A justification is required to approve over blocking issues: "
                       "write why paying is right despite them. It goes on the audit record.")
            if decision == "APPROVE" and has_blocking and not justification.strip():
                return render_page(request, conn, row, 400, error=message, form=form)
            # A crash after paying but before saving leaves the row paused while the checkpoint says PAID:
            # record that result and show it, never resume (and pay) the run a second time.
            paid = _paid_checkpoint(graph, run_id)
            if paid is not None:
                db.save_run(deps.conn, paid)
                return render_page(request, conn, db.get_run(conn, run_id) or row, 409,
                                     notice="This invoice had already been paid, so it wasn't paid again.")
            try:
                # deps.conn (not this request's connection): the graph's own writes go through it, one decision at a time.
                resume_review(run_id, decision, note.strip(), justification.strip() or None, deps, graph)
            except ValueError as exc:
                # Defence in depth: the graph's own resume schema also refuses an unjustified approval.
                if "justification required" not in str(exc):
                    raise
                return render_page(request, conn, row, 400, error=message, form=form)
            except Exception:
                # E.g. a missing or corrupt checkpoint: log the detail, show the reviewer a plain notice.
                log.exception("decision failed", extra={"fields": {"run_id": run_id}})
                return render_page(request, conn, db.get_run(conn, run_id) or row, 500, form=form, notice=(
                    "We couldn't record that decision: the run's saved progress is missing or damaged. "
                    "Check the Invoices list before trying again."))
        return RedirectResponse(f"/invoices/{run_id}?decided=1", status_code=303)

    return app

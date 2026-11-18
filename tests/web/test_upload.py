"""Tests for uploading invoices and watching their story live: upload checks, background jobs, the /status endpoint,
the "run all sample invoices" button and startup recovery. Fake deps/graph stand in for Grok; jobs are joined to finish.
"""
import pytest
from fastapi.testclient import TestClient

from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
from invoice_agent.models import FinalOutcome, Invoice
from invoice_agent.runner import process_file
from invoice_agent.utils.reader import SUPPORTED_SUFFIXES
from invoice_agent.web import app as web_app
from invoice_agent.web.app import MAX_UPLOAD_BYTES, create_app
from tests.fakes import make_extracted
from tests.test_runner import INVOICES, build

SAME_ORIGIN = "http://testserver"


@pytest.fixture
def conn(tmp_path):
    """Fresh seeded app database for each test, at the path the app's settings point to."""
    c = db.connect(tmp_path / "app.db")
    db.init_db(c)
    yield c
    c.close()


def make_client(conn, tmp_path, extractions=None):
    """A TestClient whose background jobs each get their own fake runtime over the same tmp database."""
    settings = Settings(var_dir=tmp_path)

    def factory():
        """A fresh fake runtime with its own connection, as each real background job gets."""
        deps, graph, _ = build(db.connect(settings.app_db), extractions or [make_extracted()] * 40)
        return deps, graph

    deps, graph, _ = build(conn, [])
    app = create_app(deps, graph, conn, settings=settings, runtime_factory=factory)
    client = TestClient(app, follow_redirects=False, headers={"origin": SAME_ORIGIN})
    return client, app


def wait_for_jobs(app):
    """Block until every background job the app started has finished."""
    for job in list(app.state.jobs):
        job.join(timeout=30)
        assert not job.is_alive(), "background job did not finish"


def upload(client, name, content):
    """POST a file to /upload as the queue page's form would."""
    return client.post("/upload", files={"file": (name, content, "text/plain")})


def test_upload_rejects_bad_extension(conn, tmp_path):
    """A .exe upload is refused with 400 and a page listing the file types we accept; nothing is saved."""
    client, _ = make_client(conn, tmp_path)
    resp = upload(client, "invoice.exe", b"MZ")
    assert resp.status_code == 400
    assert "PDF, TXT, JSON, CSV or XML" in resp.text
    assert db.list_runs(conn) == []


def test_upload_rejects_oversize(conn, tmp_path):
    """A file over 5 MB is refused with 413 and no run is created."""
    client, _ = make_client(conn, tmp_path)
    resp = upload(client, "big.txt", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert resp.status_code == 413
    assert "5 MB" in resp.text
    assert db.list_runs(conn) == []


def test_upload_sanitizes_filename(conn, tmp_path):
    """A name like ../../evil.txt is saved inside var/uploads/ as <run_id>_evil.txt and never escapes it."""
    client, app = make_client(conn, tmp_path)
    resp = upload(client, "../../evil.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    run_id = resp.headers["location"].rsplit("/", 1)[1]
    uploads = tmp_path / "uploads"
    assert [p.name for p in uploads.iterdir()] == [f"{run_id}_evil.txt"]
    assert not (tmp_path.parent / "evil.txt").exists() and not (tmp_path / "evil.txt").exists()


def test_upload_creates_run_and_redirects(conn, tmp_path):
    """A good upload creates a PROCESSING run straight away and 303s to its detail page."""
    client, app = make_client(conn, tmp_path)
    resp = upload(client, "invoice_1001.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    assert resp.status_code == 303
    run_id = resp.headers["location"].removeprefix("/invoices/")
    assert db.get_run(conn, run_id) is not None
    wait_for_jobs(app)


def test_status_endpoint_reports_stage(conn, tmp_path):
    """After the background job finishes, /status reports the final status and the last stage that ran."""
    client, app = make_client(conn, tmp_path)
    resp = upload(client, "invoice_1001.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    status = client.get(resp.headers["location"] + "/status").json()
    assert status["status"] == "PAID" and status["stage"] == "pay"


def test_status_of_unknown_run_is_404(conn, tmp_path):
    """Polling a run id that doesn't exist gives a 404, so live.js stops polling."""
    client, _ = make_client(conn, tmp_path)
    assert client.get("/invoices/nope/status").status_code == 404


def test_running_story_is_live(conn, tmp_path):
    """A run still PROCESSING shows its finished steps, the next one running, and polls via live.js."""
    client, _ = make_client(conn, tmp_path)
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    db.save_progress(conn, "r1", "read", {"run_id": "r1", "raw_text": "line\nline", "format": "txt"})
    html = client.get("/invoices/r1").text
    assert 'data-live-run="r1"' in html and 'aria-live="polite"' in html and "live.js" in html
    assert "Text invoice, 2 lines" in html and "Ingestion agent is reading it..." in html
    assert "st-running" in html and ">Running<" in html and "invoice.txt" in html


def test_main_page_offers_intake_and_lists_running_run(conn, tmp_path):
    """The main page offers the upload form and samples button, and lists a PROCESSING run as Running."""
    client, _ = make_client(conn, tmp_path)
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    db.set_stage(conn, "r1", "extract")
    html = client.get("/dashboard").text
    assert 'action="/upload"' in html and 'enctype="multipart/form-data"' in html
    assert "Run all 20 sample invoices" in html
    assert ">Running<" in html and "invoice.txt" in html


def test_upload_redirects_to_its_story(conn, tmp_path):
    """After an upload the browser lands on that invoice's story, which ends paid once the job is done."""
    client, app = make_client(conn, tmp_path)
    resp = upload(client, "invoice_1001.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    html = client.get(resp.headers["location"]).text
    assert "invoice_1001.txt" in html and "7. Decision: Paid" in html and "data-live-run" not in html


def test_progress_is_never_written_over_a_finished_run(conn):
    """A late progress save leaves a finished run's result alone."""
    db.save_run(conn, {"run_id": "r1", "final": FinalOutcome(status="PAID", decided_by="agents", reasoning="ok")})
    db.save_progress(conn, "r1", "read", {"run_id": "r1"})
    assert db.get_run(conn, "r1")["state"]["final"]["status"] == "PAID"


def test_cross_origin_upload_is_403(conn, tmp_path):
    """An upload posted from another site is blocked by the same-origin guard and creates nothing."""
    client, _ = make_client(conn, tmp_path)
    resp = client.post("/upload", files={"file": ("a.txt", b"x", "text/plain")}, headers={"origin": "https://evil.example"})
    assert resp.status_code == 403
    assert db.list_runs(conn) == [] and not (tmp_path / "uploads").exists()


def test_reupload_of_paid_file_opens_existing_run(conn, tmp_path):
    """Uploading a file that was already paid opens that run instead of processing (and paying) it again."""
    client, app = make_client(conn, tmp_path)
    first = upload(client, "invoice_1001.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    jobs_before = len(app.state.jobs)
    again = upload(client, "copy.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    assert again.headers["location"].startswith(first.headers["location"])
    assert len(app.state.jobs) == jobs_before and len(db.list_runs(conn)) == 1


def test_process_samples_runs_every_sample(conn, tmp_path):
    """The samples button 303s to the dashboard and processes every supported file in data/invoices, none left PROCESSING."""
    client, app = make_client(conn, tmp_path)
    resp = client.post("/process-samples")
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    wait_for_jobs(app)
    expected = sum(1 for p in web_app.SAMPLES_DIR.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    runs = db.list_runs(conn)
    assert len(runs) == expected
    assert all(r["status"] != "PROCESSING" for r in runs)


def test_failed_runtime_marks_run_error(conn, tmp_path):
    """If the background job can't start its runtime (e.g. no API key), the run ends as ERROR, not stuck PROCESSING."""
    settings = Settings(var_dir=tmp_path)

    def broken():
        """A runtime factory that fails like build_runtime does without a key."""
        raise RuntimeError("XAI_API_KEY is not set")

    deps, graph, _ = build(conn, [])
    app = create_app(deps, graph, conn, settings=settings, runtime_factory=broken)
    client = TestClient(app, follow_redirects=False, headers={"origin": SAME_ORIGIN})
    resp = upload(client, "invoice_1001.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    row = db.get_run(conn, resp.headers["location"].removeprefix("/invoices/"))
    assert row["status"] == "ERROR" and "XAI_API_KEY" in row["state"]["final"]["reasoning"]


def test_startup_marks_interrupted_runs_error(conn, tmp_path):
    """A run left PROCESSING by a killed server is marked ERROR with a plain "re-upload" message when the app starts."""
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    make_client(conn, tmp_path)
    row = db.get_run(conn, "r1")
    assert row["status"] == "ERROR"
    assert row["state"]["final"]["reasoning"] == "Processing was interrupted: re-upload the file to try again."


def test_init_db_adds_stage_column_to_old_database(tmp_path):
    """An app.db created before the stage column existed gains it on init, keeping its rows."""
    c = db.connect(tmp_path / "old.db")
    c.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, source_path TEXT, content_hash TEXT, invoice_number TEXT,"
              " vendor TEXT, fingerprint TEXT, status TEXT, amount TEXT, created_at TEXT, updated_at TEXT, state_json TEXT)")
    c.execute("INSERT INTO runs (run_id, status, state_json) VALUES ('old', 'PAID', '{}')")
    db.init_db(c)
    db.set_stage(c, "old", "pay")
    assert db.get_run(c, "old")["stage"] == "pay"
    c.close()


def test_process_file_reports_stages(conn):
    """on_update hears each top-level graph node as it finishes, in order, and a preassigned run id is used."""
    deps, graph, _ = build(conn, [make_extracted()])
    seen: list[str] = []

    def on_update(namespace, node, update):
        """Keep only the top-level pipeline nodes (not the analyst/VP loop inside approval)."""
        if not namespace:
            seen.append(node)

    state = process_file(INVOICES / "invoice_1001.txt", deps, graph, run_id="fixed-id", on_update=on_update)
    assert seen[:4] == ["read", "extract", "validate", "approval"]
    assert seen[-1] == "pay" and state["run_id"] == "fixed-id"


# ---------- fix round 1 ----------

def test_failed_rerun_keeps_content_hash_and_reupload_finds_same_run(conn, tmp_path):
    """An ERROR run re-run by upload whose runtime fails keeps its content_hash, so the next upload reuses its id."""
    import hashlib

    content = (INVOICES / "invoice_1001.txt").read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    db.save_run(conn, {"run_id": "old", "source_path": "data/invoices/invoice_1001.txt", "content_hash": digest,
                       "final": FinalOutcome(status="ERROR", decided_by="system", reasoning="Request timed out.")})

    def broken():
        """A runtime factory that fails like build_runtime does without a key."""
        raise RuntimeError("XAI_API_KEY is not set")

    deps, graph, _ = build(conn, [])
    app = create_app(deps, graph, conn, settings=Settings(var_dir=tmp_path), runtime_factory=broken)
    client = TestClient(app, follow_redirects=False, headers={"origin": SAME_ORIGIN})
    first = upload(client, "invoice_1001.txt", content)
    wait_for_jobs(app)
    assert first.headers["location"] == "/invoices/old"
    assert db.get_run(conn, "old")["content_hash"] == digest
    again = upload(client, "invoice_1001.txt", content)
    wait_for_jobs(app)
    assert again.headers["location"] == "/invoices/old" and len(db.list_runs(conn)) == 1


def test_partial_save_never_erases_identity_columns(conn):
    """Re-saving a run with a state that lacks the hash, number, vendor or fingerprint keeps the stored ones."""
    db.save_run(conn, {"run_id": "r1", "source_path": "a.txt", "content_hash": "h1", "fingerprint": "fp",
                       "invoice": make_invoice(), "final": FinalOutcome(status="REJECTED", decided_by="agents",
                                                                       reasoning="x")})
    db.save_run(conn, {"run_id": "r1", "final": FinalOutcome(status="ERROR", decided_by="system", reasoning="y")})
    row = db.get_run(conn, "r1")
    assert (row["source_path"], row["content_hash"], row["fingerprint"], row["invoice_number"], row["vendor"]) == \
        ("a.txt", "h1", "fp", "INV-1", "Widgets Inc.")
    assert row["status"] == "ERROR"


def make_invoice():
    """A minimal invoice with a number and vendor, for identity-column tests."""
    return Invoice(invoice_number="INV-1", vendor="Widgets Inc.")


def test_long_filename_is_truncated_and_saved(conn, tmp_path):
    """A 300-character file name is accepted, its stem cut to 100 characters and the extension kept."""
    client, app = make_client(conn, tmp_path)
    resp = upload(client, "a" * 300 + ".txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    assert resp.status_code == 303
    run_id = resp.headers["location"].removeprefix("/invoices/")
    assert [p.name for p in (tmp_path / "uploads").iterdir()] == [f"{run_id}_{'a' * 100}.txt"]


@pytest.mark.parametrize("name", [".txt", "...", "../.txt"])
def test_nameless_upload_is_400(conn, tmp_path, name):
    """A name that sanitizes to nothing (a bare extension or only dots) is refused with 400 and nothing saved."""
    client, _ = make_client(conn, tmp_path)
    resp = upload(client, name, b"Invoice")
    assert resp.status_code == 400
    assert db.list_runs(conn) == [] and not (tmp_path / "uploads").exists()


def test_oversize_content_length_is_413_before_parsing(conn, tmp_path):
    """A request declaring a body far over 5 MB is refused with 413 by its Content-Length, before the form is read."""
    client, _ = make_client(conn, tmp_path)
    resp = client.post("/upload", content=b"x", headers={
        "content-type": "multipart/form-data; boundary=b", "content-length": str(MAX_UPLOAD_BYTES * 2)})
    assert resp.status_code == 413 and "5 MB" in resp.text


def test_finished_jobs_are_pruned(conn, tmp_path):
    """Starting a new job drops threads that have finished, so the list doesn't grow with every upload."""
    client, app = make_client(conn, tmp_path)
    upload(client, "a.txt", (INVOICES / "invoice_1001.txt").read_bytes())
    wait_for_jobs(app)
    upload(client, "b.txt", (INVOICES / "invoice_1002.txt").read_bytes())
    assert len(app.state.jobs) == 1
    wait_for_jobs(app)


def test_final_outcome_rejects_processing():
    """PROCESSING is a run-row status only: a FinalOutcome can never claim it."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FinalOutcome(status="PROCESSING", decided_by="system", reasoning="x")


def test_reupload_while_processing_says_in_progress(conn, tmp_path):
    """Uploading a file whose run is still processing opens it with an "already being processed" notice."""
    import hashlib

    content = (INVOICES / "invoice_1001.txt").read_bytes()
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice_1001.txt", hashlib.sha256(content).hexdigest())
    client, _ = make_client(conn, tmp_path)
    # The app's startup recovery marked r1 ERROR; put it back in progress, as a live job would hold it.
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice_1001.txt")
    resp = upload(client, "invoice_1001.txt", content)
    assert resp.headers["location"] == "/invoices/r1?seen=1"
    assert "already being processed" in client.get(resp.headers["location"]).text

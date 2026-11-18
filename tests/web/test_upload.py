"""Tests for uploading invoices and watching them run live: upload checks, background jobs, the /status endpoint,
the "process all samples" button and startup recovery. Fake deps/graph stand in for Grok; jobs are joined to finish.
"""
import pytest
from fastapi.testclient import TestClient

from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
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


def test_processing_detail_shows_tracker(conn, tmp_path):
    """A run still PROCESSING shows the plain-language stage tracker with the current step marked and a live region."""
    client, _ = make_client(conn, tmp_path)
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    db.set_stage(conn, "r1", "read")
    html = client.get("/invoices/r1").text
    assert "Reading file" in html and "Checking against inventory" in html and "Analyst ⇄ VP review" in html
    assert 'aria-current="step"' in html and 'aria-live="polite"' in html
    # read has finished, so extraction is the step in progress.
    assert html.index('aria-current="step"') > html.index("Reading file")
    assert html.index('aria-current="step"') < html.index("Checking against inventory")
    assert "live.js" in html and "invoice.txt" in html


def test_queue_shows_processing_stage_and_intake(conn, tmp_path):
    """The queue offers the upload form and samples button, and a PROCESSING row shows its current stage."""
    client, _ = make_client(conn, tmp_path)
    db.create_pending_run(conn, "r1", "var/uploads/r1_invoice.txt")
    db.set_stage(conn, "r1", "extract")
    html = client.get("/").text
    assert 'action="/upload"' in html and 'enctype="multipart/form-data"' in html
    assert "Process all sample invoices" in html
    assert "Processing" in html and "Checking against inventory" in html


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
    """The samples button 303s to the queue and processes every supported file in data/invoices, none left PROCESSING."""
    client, app = make_client(conn, tmp_path)
    resp = client.post("/process-samples")
    assert resp.status_code == 303 and resp.headers["location"] == "/"
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
    assert row["state"]["final"]["reasoning"] == "Processing was interrupted — re-upload the file to try again."


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
    """on_stage hears each graph node as it finishes, in order, and a preassigned run id is used."""
    deps, graph, _ = build(conn, [make_extracted()])
    seen: list[str] = []
    state = process_file(INVOICES / "invoice_1001.txt", deps, graph, run_id="fixed-id", on_stage=seen.append)
    assert seen[:4] == ["read", "extract", "validate", "approval"]
    assert seen[-1] == "pay" and state["run_id"] == "fixed-id"

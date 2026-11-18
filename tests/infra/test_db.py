"""Tests for SQLite inventory seeding and run history (save, lookup, ordering, duplicate lookups, reset).
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from invoice_agent.infra import db
from invoice_agent.infra.config import Settings
from invoice_agent.models import FinalOutcome, Invoice


def _conn(tmp_path):
    """Fresh initialized DB in a temp dir (also checks parent dirs are created)."""
    conn = db.connect(tmp_path / "sub" / "app.db")
    db.init_db(conn)
    return conn


def _state(run_id, status=None, number="INV-1", **extra):
    """Build a minimal pipeline state for saving."""
    state = {"run_id": run_id, "invoice": Invoice(invoice_number=number, vendor="Acme", total="100.50")}
    if status:
        state["final"] = FinalOutcome(status=status, decided_by="agents", reasoning="ok")
    state.update(extra)
    return state


def test_init_seeds_inventory_idempotently(tmp_path):
    """Seeding runs once, can repeat safely, and unknown SKUs return None."""
    conn = _conn(tmp_path)
    db.init_db(conn)
    assert db.get_inventory(conn, "GadgetX") == {"item": "GadgetX", "stock": 5, "unit_price": Decimal("750.00")}
    assert len(db.all_skus(conn)) == 4
    assert db.get_inventory(conn, "Nope") is None


def test_save_and_get_run_roundtrip(tmp_path):
    """A saved run reads back intact; re-saving updates status but keeps created_at."""
    conn = _conn(tmp_path)
    db.save_run(conn, _state("r1", "PAID", content_hash="h1", fingerprint="f1"))
    run = db.get_run(conn, "r1")
    assert run["status"] == "PAID"
    assert run["state"]["final"]["status"] == "PAID"
    assert run["state"]["invoice"]["total"] == "100.50"
    assert run["amount"] == "100.50" and run["vendor"] == "Acme"
    created = run["created_at"]
    db.save_run(conn, _state("r1", "REJECTED"))
    again = db.get_run(conn, "r1")
    assert again["status"] == "REJECTED" and again["created_at"] == created
    assert db.get_run(conn, "missing") is None


def test_list_runs_puts_needs_review_first(tmp_path):
    """Runs needing human review sort above the rest."""
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID"))
    db.save_run(conn, _state("b", "NEEDS_HUMAN_REVIEW"))
    db.save_run(conn, _state("c", "REJECTED"))
    assert [r["run_id"] for r in db.list_runs(conn)] == ["b", "c", "a"]


def test_save_progress_stamps_when_each_step_starts(tmp_path):
    """A new stage stamps step_started_at (kept for later saves in the same step); new_step=True stamps without a stage."""
    conn = _conn(tmp_path)
    db.create_pending_run(conn, "r1", "x.txt", "h1")
    partial = {"run_id": "r1"}
    db.save_progress(conn, "r1", "read", partial)
    first = db.get_run(conn, "r1")["state"]["step_started_at"]
    # A tool lookup inside the same step keeps the start time.
    db.save_progress(conn, "r1", None, partial)
    assert db.get_run(conn, "r1")["state"]["step_started_at"] == first
    partial["step_started_at"] = "2000-01-01T00:00:00+00:00"
    # The analyst finishing a round starts the VP's step: the caller says so with new_step=True.
    db.save_progress(conn, "r1", None, partial, new_step=True)
    stamped = db.get_run(conn, "r1")["state"]["step_started_at"]
    assert stamped > "2000-01-01" and stamped.endswith("+00:00")


def test_find_runs_by_invoice_number_excludes_self(tmp_path):
    """Invoice-number lookup omits the current run (double-billing check)."""
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID", number="INV-9"))
    db.save_run(conn, _state("b", "PAID", number="INV-9"))
    db.save_run(conn, _state("c", "PAID", number="INV-2"))
    assert [r["run_id"] for r in db.find_runs_by_invoice_number(conn, "INV-9", "a")] == ["b"]
    assert len(db.find_runs_by_invoice_number(conn, "INV-9", None)) == 2


def test_find_run_by_content_hash(tmp_path):
    """Exact-duplicate lookup by file hash."""
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID", content_hash="abc"))
    assert db.find_run_by_content_hash(conn, "abc")["run_id"] == "a"
    assert db.find_run_by_content_hash(conn, "zzz") is None


def test_reset_deletes_db_files(tmp_path):
    """reset removes both DB files, their -wal/-shm sidecars and uploads, and tolerates already-missing files."""
    s = Settings(var_dir=tmp_path)
    files = [s.app_db, s.checkpoint_db] + [p.with_name(p.name + x) for p in (s.app_db, s.checkpoint_db)
                                           for x in ("-wal", "-shm")]
    for f in files:
        f.write_text("x")
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "a_invoice.txt").write_text("x")
    db.reset(s)
    assert not any(f.exists() for f in files) and not (tmp_path / "uploads").exists()
    db.reset(s)


def test_connect_uses_wal_and_busy_timeout(tmp_path):
    """Connections wait for a busy writer (5 s) and use WAL so web requests and workers can share the file."""
    conn = _conn(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_connection_usable_from_worker_thread(tmp_path):
    """Agent tools run on LangChain worker threads, so the connection must work outside the thread that opened it."""
    conn = _conn(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        row = pool.submit(db.get_inventory, conn, "WidgetA").result()
    assert row["stock"] == 15

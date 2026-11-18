from decimal import Decimal

from invoice_agent import db
from invoice_agent.config import Settings
from invoice_agent.models import FinalOutcome, Invoice


def _conn(tmp_path):
    conn = db.connect(tmp_path / "sub" / "app.db")
    db.init_db(conn)
    return conn


def _state(run_id, status=None, number="INV-1", **extra):
    state = {"run_id": run_id, "invoice": Invoice(invoice_number=number, vendor="Acme", total="100.50")}
    if status:
        state["final"] = FinalOutcome(status=status, decided_by="agents", reasoning="ok")
    state.update(extra)
    return state


def test_init_seeds_inventory_idempotently(tmp_path):
    conn = _conn(tmp_path)
    db.init_db(conn)
    assert db.get_inventory(conn, "GadgetX") == {"item": "GadgetX", "stock": 5, "unit_price": Decimal("750.00")}
    assert len(db.all_skus(conn)) == 4
    assert db.get_inventory(conn, "Nope") is None


def test_save_and_get_run_roundtrip(tmp_path):
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
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID"))
    db.save_run(conn, _state("b", "NEEDS_HUMAN_REVIEW"))
    db.save_run(conn, _state("c", "REJECTED"))
    assert [r["run_id"] for r in db.list_runs(conn)] == ["b", "c", "a"]


def test_find_runs_by_invoice_number_excludes_self(tmp_path):
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID", number="INV-9"))
    db.save_run(conn, _state("b", "PAID", number="INV-9"))
    db.save_run(conn, _state("c", "PAID", number="INV-2"))
    assert [r["run_id"] for r in db.find_runs_by_invoice_number(conn, "INV-9", "a")] == ["b"]
    assert len(db.find_runs_by_invoice_number(conn, "INV-9", None)) == 2


def test_find_run_by_content_hash(tmp_path):
    conn = _conn(tmp_path)
    db.save_run(conn, _state("a", "PAID", content_hash="abc"))
    assert db.find_run_by_content_hash(conn, "abc")["run_id"] == "a"
    assert db.find_run_by_content_hash(conn, "zzz") is None


def test_reset_deletes_db_files(tmp_path):
    s = Settings(var_dir=tmp_path)
    s.app_db.write_text("x")
    s.checkpoint_db.write_text("y")
    db.reset(s)
    assert not s.app_db.exists() and not s.checkpoint_db.exists()
    db.reset(s)

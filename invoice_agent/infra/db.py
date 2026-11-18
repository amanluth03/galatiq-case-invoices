"""SQLite persistence: seeded inventory table plus run history (full state as JSON).
Used by validate (stock/price/duplicate checks), pay, and the review screen (which also tracks live run stages here).
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from invoice_agent.infra.config import Settings
from invoice_agent.models import PipelineState

# Fixed seed stock and reference prices (FakeItem has zero stock on purpose: sample invoice 1003).
# Prices are stored as TEXT so Decimal round-trips exactly.
SEED_INVENTORY = [
    ("WidgetA", 15, "250.00"),
    ("WidgetB", 10, "500.00"),
    ("GadgetX", 5, "750.00"),
    ("FakeItem", 0, "0.00"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    item TEXT PRIMARY KEY, stock INTEGER, unit_price TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, source_path TEXT, content_hash TEXT, invoice_number TEXT,
    vendor TEXT, fingerprint TEXT, status TEXT, amount TEXT,
    created_at TEXT, updated_at TEXT, state_json TEXT, stage TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the SQLite file (creating parent dirs) with dict-like rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: LangChain runs agent tools (get_inventory, ...) on worker threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and seed inventory; safe to call repeatedly."""
    conn.executescript(SCHEMA)
    # Migration: an app.db made before live progress existed has no stage column; add it, keeping every row.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    if "stage" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN stage TEXT")
    # INSERT OR IGNORE keeps init idempotent and never resets stock that has changed since.
    conn.executemany("INSERT OR IGNORE INTO inventory (item, stock, unit_price) VALUES (?, ?, ?)", SEED_INVENTORY)
    conn.commit()


def get_inventory(conn: sqlite3.Connection, item: str) -> dict | None:
    """Return stock and reference price for a SKU, or None if unknown."""
    row = conn.execute("SELECT item, stock, unit_price FROM inventory WHERE item = ?", (item,)).fetchone()
    if row is None:
        return None
    return {"item": row["item"], "stock": row["stock"], "unit_price": Decimal(row["unit_price"])}


def all_skus(conn: sqlite3.Connection) -> list[str]:
    """All known SKUs, used for matching invoice item names."""
    return [r["item"] for r in conn.execute("SELECT item FROM inventory ORDER BY item")]


def _jsonable(value):
    """Recursively convert models and Decimals into plain JSON types."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    # Decimal becomes a string (not float) so cents survive the JSON round trip.
    if isinstance(value, Decimal):
        return str(value)
    return value


def _now() -> str:
    """Current UTC time as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


def save_run(conn: sqlite3.Connection, state: PipelineState) -> None:
    """Insert or update a run row, denormalizing key fields for querying and keeping full state as JSON."""
    data = _jsonable(dict(state))
    invoice = data.get("invoice") or {}
    final = data.get("final") or {}
    now = _now()
    # Upsert: re-saving a run updates it but keeps its original created_at.
    # COALESCE: a partial save (e.g. an ERROR before the file was read) must never erase a run's identity columns;
    # losing content_hash would let a re-upload start a new run and break the never-pay-twice recovery.
    conn.execute(
        """
        INSERT INTO runs (run_id, source_path, content_hash, invoice_number, vendor, fingerprint,
                          status, amount, created_at, updated_at, state_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            source_path=COALESCE(excluded.source_path, runs.source_path),
            content_hash=COALESCE(excluded.content_hash, runs.content_hash),
            invoice_number=COALESCE(excluded.invoice_number, runs.invoice_number),
            vendor=COALESCE(excluded.vendor, runs.vendor),
            fingerprint=COALESCE(excluded.fingerprint, runs.fingerprint), status=excluded.status, amount=excluded.amount,
            updated_at=excluded.updated_at, state_json=excluded.state_json
        """,
        (
            data["run_id"], data.get("source_path"), data.get("content_hash"),
            invoice.get("invoice_number"), invoice.get("vendor"), data.get("fingerprint"),
            final.get("status"), invoice.get("total"), now, now, json.dumps(data),
        ),
    )
    conn.commit()


def create_pending_run(conn: sqlite3.Connection, run_id: str, source_path: str,
                       content_hash: str | None = None) -> None:
    """Mark a run PROCESSING before the pipeline starts, so the screen can show it straight away."""
    now = _now()
    # On a re-run of a known file the row already exists: keep its content_hash and created_at, reset the progress.
    conn.execute(
        """
        INSERT INTO runs (run_id, source_path, content_hash, status, stage, created_at, updated_at, state_json)
        VALUES (?, ?, ?, 'PROCESSING', NULL, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            source_path=excluded.source_path, content_hash=COALESCE(runs.content_hash, excluded.content_hash),
            status='PROCESSING', stage=NULL, updated_at=excluded.updated_at
        """,
        (run_id, source_path, content_hash, now, now, json.dumps({"run_id": run_id, "source_path": source_path})),
    )
    conn.commit()


def record_identity(conn: sqlite3.Connection, run_id: str, source_path: str, content_hash: str | None,
                    invoice_number: str | None, fingerprint: str) -> None:
    """Store a run's invoice number and fingerprint as soon as it is extracted, so a parallel run's duplicate check sees it."""
    now = _now()
    # A new run gets a PROCESSING row; an existing one (a re-run, or a web upload's pending row) keeps its status.
    conn.execute(
        """
        INSERT INTO runs (run_id, source_path, content_hash, invoice_number, fingerprint, status,
                          created_at, updated_at, state_json)
        VALUES (?, ?, ?, ?, ?, 'PROCESSING', ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            content_hash=COALESCE(runs.content_hash, excluded.content_hash),
            invoice_number=excluded.invoice_number, fingerprint=excluded.fingerprint, updated_at=excluded.updated_at
        """,
        (run_id, source_path, content_hash, invoice_number, fingerprint, now, now,
         json.dumps({"run_id": run_id, "source_path": source_path})),
    )
    conn.commit()


def set_stage(conn: sqlite3.Connection, run_id: str, stage: str) -> None:
    """Record the pipeline stage a run has reached (the graph node that just finished), for the live tracker."""
    conn.execute("UPDATE runs SET stage = ? WHERE run_id = ?", (stage, run_id))
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Turn a runs row into a dict, decoding state_json into "state"."""
    out = dict(row)
    out["state"] = json.loads(out.pop("state_json") or "{}")
    return out


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """Fetch one run by id, or None."""
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    """All runs: in progress first, then those awaiting human review, then newest."""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY (status = 'PROCESSING') DESC, (status = 'NEEDS_HUMAN_REVIEW') DESC, "
        "updated_at DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_runs_by_invoice_number(
    conn: sqlite3.Connection, invoice_number: str, exclude_run_id: str | None
) -> list[dict]:
    """Other runs sharing an invoice number (double-billing detection, e.g. 1004 vs 1004_revised); excludes the current run."""
    rows = conn.execute(
        "SELECT * FROM runs WHERE invoice_number = ? AND (? IS NULL OR run_id != ?) ORDER BY updated_at DESC",
        (invoice_number, exclude_run_id, exclude_run_id),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_run_by_content_hash(conn: sqlite3.Connection, content_hash: str) -> dict | None:
    """Find a prior run of byte-identical file content (exact duplicate)."""
    row = conn.execute("SELECT * FROM runs WHERE content_hash = ? LIMIT 1", (content_hash,)).fetchone()
    return _row_to_dict(row) if row else None


def reset(settings: Settings) -> None:
    """Delete the app and checkpoint databases for a clean start."""
    settings.app_db.unlink(missing_ok=True)
    settings.checkpoint_db.unlink(missing_ok=True)

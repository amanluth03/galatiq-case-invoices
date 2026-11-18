import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from invoice_agent.config import Settings
from invoice_agent.models import PipelineState

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
    created_at TEXT, updated_at TEXT, state_json TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR IGNORE INTO inventory (item, stock, unit_price) VALUES (?, ?, ?)", SEED_INVENTORY)
    conn.commit()


def get_inventory(conn: sqlite3.Connection, item: str) -> dict | None:
    row = conn.execute("SELECT item, stock, unit_price FROM inventory WHERE item = ?", (item,)).fetchone()
    if row is None:
        return None
    return {"item": row["item"], "stock": row["stock"], "unit_price": Decimal(row["unit_price"])}


def all_skus(conn: sqlite3.Connection) -> list[str]:
    return [r["item"] for r in conn.execute("SELECT item FROM inventory ORDER BY item")]


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_run(conn: sqlite3.Connection, state: PipelineState) -> None:
    data = _jsonable(dict(state))
    invoice = data.get("invoice") or {}
    final = data.get("final") or {}
    now = _now()
    conn.execute(
        """
        INSERT INTO runs (run_id, source_path, content_hash, invoice_number, vendor, fingerprint,
                          status, amount, created_at, updated_at, state_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            source_path=excluded.source_path, content_hash=excluded.content_hash,
            invoice_number=excluded.invoice_number, vendor=excluded.vendor,
            fingerprint=excluded.fingerprint, status=excluded.status, amount=excluded.amount,
            updated_at=excluded.updated_at, state_json=excluded.state_json
        """,
        (
            data["run_id"], data.get("source_path"), data.get("content_hash"),
            invoice.get("invoice_number"), invoice.get("vendor"), data.get("fingerprint"),
            final.get("status"), invoice.get("total"), now, now, json.dumps(data),
        ),
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["state"] = json.loads(out.pop("state_json") or "{}")
    return out


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY (status = 'NEEDS_HUMAN_REVIEW') DESC, updated_at DESC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_runs_by_invoice_number(
    conn: sqlite3.Connection, invoice_number: str, exclude_run_id: str | None
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs WHERE invoice_number = ? AND (? IS NULL OR run_id != ?) ORDER BY updated_at DESC",
        (invoice_number, exclude_run_id, exclude_run_id),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_run_by_content_hash(conn: sqlite3.Connection, content_hash: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE content_hash = ? LIMIT 1", (content_hash,)).fetchone()
    return _row_to_dict(row) if row else None


def reset(settings: Settings) -> None:
    settings.app_db.unlink(missing_ok=True)
    settings.checkpoint_db.unlink(missing_ok=True)

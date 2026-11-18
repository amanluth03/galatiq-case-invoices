"""Runs the full rulebook and detects duplicate / reused invoice numbers using a format-independent fingerprint.
Sits at the validate stage; the graph calls claim_identity (the duplicate check) separately from run_validation.
"""
import hashlib
import json
import sqlite3
import threading
from decimal import Decimal

from pydantic import BaseModel

from invoice_agent.database.db import find_runs_by_invoice_number, record_identity
from invoice_agent.models import Flag, Invoice
from invoice_agent.utils.validation import checks

_CENT = Decimal("0.01")

# Batch workers validate in parallel: only one "have I seen this invoice before?" decision at a time in this process.
_IDENTITY_LOCK = threading.Lock()


def _money(d: Decimal | None) -> str:
    """Money to two decimals so 250, 250.0 and 250.00 fingerprint the same."""
    return "" if d is None else str(d.quantize(_CENT))


def _qty(d: Decimal | None) -> str:
    """Quantity in normalized plain notation so 6, 6.0 and 6.00 match."""
    return "" if d is None else format(d.normalize(), "f")


def fingerprint(inv: Invoice) -> str:
    """Hash of vendor, total and line contents; identical for the same invoice read from PDF or TXT."""
    items = sorted((it.sku or it.name_raw, _qty(it.qty), _money(it.unit_price)) for it in inv.items)
    payload = ((inv.vendor or "").casefold().strip(), _money(inv.total), items)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


class DuplicateCheck(BaseModel):
    """Outcome of duplicate detection: which earlier run this duplicates (if any) plus any flags."""
    duplicate_of: str | None = None
    flags: list[Flag] = []


def find_duplicate(conn: sqlite3.Connection, inv: Invoice, fp: str, content_hash: str, run_id: str) -> DuplicateCheck:
    """Compare against prior runs with the same invoice number: same contents = duplicate, different = reuse."""
    if inv.invoice_number is None:
        return DuplicateCheck(duplicate_of=None, flags=[])
    priors = find_runs_by_invoice_number(conn, inv.invoice_number, exclude_run_id=run_id)
    # A DUPLICATE run is a copy, never an original: re-running the real original (e.g. after a timeout) must not
    # make it a duplicate of its own copy.
    priors = [p for p in priors if p["status"] != "DUPLICATE"]
    # Re-processing the same file reuses run_id, so exclude_run_id already skips it; a same-hash prior
    # under another run_id is a real duplicate and must not be dropped.
    if not priors:
        return DuplicateCheck(duplicate_of=None, flags=[])
    same = [p for p in priors if p["fingerprint"] == fp]
    if same:
        # Rows come newest-first; the earliest is the original submission.
        earliest = min(same, key=lambda p: p["created_at"])
        return DuplicateCheck(duplicate_of=earliest["run_id"], flags=[])
    # 1004 vs 1004_revised: same number, different contents.
    flag = Flag(
        severity="must_reject", code="invoice_number_reused", field_path="invoice_number",
        message=f"Invoice number {inv.invoice_number} was already processed with different contents: possible double-billing",
    )
    return DuplicateCheck(duplicate_of=None, flags=[flag])


def claim_identity(conn: sqlite3.Connection, inv: Invoice, fp: str, content_hash: str, run_id: str,
                   source_path: str) -> DuplicateCheck:
    """Check for duplicates and record this run's invoice number + fingerprint in one locked step.
    Two copies validated at the same moment can't both see "no prior run": exactly one becomes the original."""
    with _IDENTITY_LOCK:
        dup = find_duplicate(conn, inv, fp, content_hash, run_id)
        record_identity(conn, run_id, source_path, content_hash, inv.invoice_number, fp)
    return dup


def run_validation(conn: sqlite3.Connection, inv: Invoice, raw_text: str, extraction_issues: list[str]) -> list[Flag]:
    """Run every check and turn extraction issues into flags; duplicates are handled separately by the graph."""
    flags: list[Flag] = []
    flags += checks.check_required(inv)
    flags += checks.check_quantities(inv)
    flags += checks.check_items(inv, conn)
    flags += checks.check_stock(inv, conn)
    flags += checks.check_prices(inv, conn)
    flags += checks.check_arithmetic(inv)
    flags += checks.check_dates(inv)
    flags += checks.check_currency(inv)
    flags += checks.check_amount(inv)
    flags += checks.check_red_flags(inv, raw_text)
    flags += checks.check_amounts_in_source(inv, raw_text)
    flags += [Flag(severity="needs_scrutiny", code="extraction_issue", message=m) for m in extraction_issues]
    return flags

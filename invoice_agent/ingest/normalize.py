"""Mechanical normalization of LLM output into typed values (dates, money, invoice numbers, SKUs, currency).
Runs right after extract; pure functions so it is deterministic and unit-tested. Records an Uncertainty whenever it changes or doubts something.
"""
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as dateparser
from rapidfuzz import process

from invoice_agent.models import ExtractedField, ExtractedInvoice, Invoice, LineItem, Uncertainty


def parse_date(s: str | None) -> date | None:
    """Parse a date string to a date, or None if missing, partial, or unparseable."""
    if not s or not s.strip():
        return None
    # Require a 4-digit year: otherwise dateutil would fill the year from today's date and results would change with the clock.
    if not re.search(r"\d{4}", s):
        return None
    try:
        a = dateparser.parse(s.strip(), dayfirst=False, default=datetime(1900, 1, 1))
        b = dateparser.parse(s.strip(), dayfirst=False, default=datetime(1901, 2, 2))
    except (ValueError, OverflowError):
        return None
    # Parsing twice with two different default dates detects partial input ("Jan 30") without using the wall clock.
    # differing results mean a part (year/month/day) was filled from the default
    if a != b or a.year == 1900:
        return None
    return a.date()


def parse_money(s: str | None) -> Decimal | None:
    """Parse "$3,500.00"-style text to an exact Decimal, or None if not a plain number."""
    if s is None:
        return None
    cleaned = re.sub(r"[$,\s]", "", s)
    # Plain digits only: rejects "1e30", NaN, Infinity; negatives allowed (sample 1009 has qty -5). Max 4 decimals.
    m = re.fullmatch(r"-?\d+(\.(\d+))?", cleaned)
    if not m or len(m.group(2) or "") > 4:
        return None
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return None
    # Cap magnitude so absurd values cannot break later arithmetic.
    return d if abs(d) < 10**12 else None


def normalize_invoice_number(s: str | None) -> str | None:
    """Canonicalize to INV-#### ("1002", "INV 1012", "#INV-1010"); leave digit-free values as-is."""
    if s is None or not s.strip():
        return None
    digits = re.sub(r"\D", "", s)
    return "INV-" + digits if digits else s.strip()


def _sku_key(name: str) -> str:
    """Comparison key: casefold, drop parentheticals and all non-alphanumerics."""
    # Drop "(rush order)" style notes so they do not block a match (sample 1010).
    name = re.sub(r"\([^)]*\)", "", name.casefold())
    return re.sub(r"[^a-z0-9]", "", name)


def match_sku(name: str, skus: list[str]) -> tuple[str | None, str | None]:
    """Return (exact SKU, fuzzy suggestion); at most one is set. "Widget A" -> WidgetA (sample 1012)."""
    keys = {_sku_key(s): s for s in skus}
    key = _sku_key(name)
    if key in keys:
        return keys[key], None
    # Near-misses are only suggested, never applied: a wrong auto-match could approve an unknown item.
    hit = process.extractOne(key, list(keys), score_cutoff=90)
    return (None, keys[hit[0]]) if hit else (None, None)


def normalize_currency(s: str | None) -> tuple[str, bool]:
    """Return (ISO code, defaulted) mapping $ and EUR symbols; defaulted=True when none was given."""
    if s is None or not s.strip():
        return "USD", True
    c = s.strip().upper()
    if c in ("$", "USD"):
        return "USD", False
    if c in ("€", "EUR"):
        return "EUR", False
    return c, False


def _text(f: ExtractedField | None) -> str | None:
    """Stripped text of a field, or None if absent or blank."""
    if f is None or f.value is None or not f.value.strip():
        return None
    return f.value.strip()


def to_invoice(ext: ExtractedInvoice, skus: list[str]) -> tuple[Invoice, list[Uncertainty]]:
    """Convert the LLM's ExtractedInvoice into a typed Invoice plus the list of Uncertainties."""
    unc: list[Uncertainty] = []

    def note(path: str, f: ExtractedField, normalized, parsed_ok: bool = True) -> None:
        """Record an Uncertainty if parsing failed or the LLM was not highly confident."""
        norm = None if normalized is None else str(normalized)
        if not parsed_ok:
            unc.append(Uncertainty(field_path=path, raw=f.raw, normalized=None,
                                   confidence=f.confidence, reason="could not parse"))
        elif f.confidence != "high":
            unc.append(Uncertainty(field_path=path, raw=f.raw, normalized=norm,
                                   confidence=f.confidence,
                                   reason=f"extractor confidence {f.confidence}"))

    def conv(path: str, f: ExtractedField | None, fn):
        """Convert a field with fn and note any uncertainty; absent/blank gives None."""
        if f is None or f.value is None or not f.value.strip():
            return None
        v = fn(f.value)
        note(path, f, v, parsed_ok=v is not None)
        return v

    def text(path: str, f: ExtractedField | None) -> str | None:
        """Clean a text field and note any uncertainty."""
        v = _text(f)
        if v is not None:
            note(path, f, v)
        return v

    if ext.currency is not None and _text(ext.currency) is not None:
        currency, _ = normalize_currency(ext.currency.value)
        note("currency", ext.currency, currency)
    else:
        # No currency stated: assume USD but flag it low confidence.
        currency = "USD"
        unc.append(Uncertainty(field_path="currency", raw=None, normalized="USD",
                               confidence="low", reason="currency missing, defaulted to USD"))

    items: list[LineItem] = []
    for i, it in enumerate(ext.items):
        p = f"items[{i}]"
        # NFKC folds look-alike characters (e.g. full-width letters) before matching.
        name = unicodedata.normalize("NFKC", (it.name.value or "")).strip()
        sku, suggestion = match_sku(name, skus)
        if it.name.confidence != "high":
            note(f"{p}.name", it.name, sku or name)
        # SKU found but the name changed beyond case/spacing (e.g. dropped "(rush order)"): emit an Uncertainty so the change is visible, not silent.
        if sku and re.sub(r"\s", "", name.casefold()) != sku.casefold():
            removed = " ".join(re.findall(r"\([^)]*\)", name)) or "".join(
                re.findall(r"[^\w\s]|_", name))
            unc.append(Uncertainty(field_path=f"{p}.name", raw=it.name.raw or name, normalized=sku,
                                   confidence="medium",
                                   reason=f"name normalized to {sku} (removed: '{removed}')"))
        # Fuzzy-only match: surface as a low-confidence suggestion; validation will still treat the item as unknown.
        if suggestion:
            unc.append(Uncertainty(field_path=f"{p}.name", raw=it.name.raw, normalized=suggestion,
                                   confidence="low", reason=f"no exact SKU match; closest is {suggestion}"))
        items.append(LineItem(
            name_raw=name, sku=sku, sku_suggestion=suggestion,
            qty=conv(f"{p}.qty", it.qty, parse_money),
            unit_price=conv(f"{p}.unit_price", it.unit_price, parse_money),
            line_total=conv(f"{p}.line_total", it.line_total, parse_money),
            note=text(f"{p}.note", it.note),
        ))

    inv = Invoice(
        invoice_number=conv("invoice_number", ext.invoice_number, normalize_invoice_number),
        vendor=text("vendor", ext.vendor),
        date=conv("date", ext.date, parse_date),
        due_date=conv("due_date", ext.due_date, parse_date),
        currency=currency,
        items=items,
        subtotal=conv("subtotal", ext.subtotal, parse_money),
        tax=conv("tax", ext.tax, parse_money),
        shipping=conv("shipping", ext.shipping, parse_money),
        total=conv("total", ext.total, parse_money),
        payment_terms=text("payment_terms", ext.payment_terms),
        notes=text("notes", ext.notes),
    )
    return inv, unc

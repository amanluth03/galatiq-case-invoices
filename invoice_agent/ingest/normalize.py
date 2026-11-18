import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dateutil import parser as dateparser
from rapidfuzz import process

from invoice_agent.models import ExtractedField, ExtractedInvoice, Invoice, LineItem, Uncertainty


def parse_date(s: str | None) -> date | None:
    if not s or not s.strip():
        return None
    if not re.search(r"\d{4}", s):
        return None
    try:
        a = dateparser.parse(s.strip(), dayfirst=False, default=datetime(1900, 1, 1))
        b = dateparser.parse(s.strip(), dayfirst=False, default=datetime(1901, 2, 2))
    except (ValueError, OverflowError):
        return None
    # differing results mean a part (year/month/day) was filled from the default
    if a != b or a.year == 1900:
        return None
    return a.date()


def parse_money(s: str | None) -> Decimal | None:
    if s is None:
        return None
    cleaned = re.sub(r"[$,\s]", "", s)
    m = re.fullmatch(r"-?\d+(\.(\d+))?", cleaned)
    if not m or len(m.group(2) or "") > 4:
        return None
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return None
    return d if abs(d) < 10**12 else None


def normalize_invoice_number(s: str | None) -> str | None:
    if s is None or not s.strip():
        return None
    digits = re.sub(r"\D", "", s)
    return "INV-" + digits if digits else s.strip()


def _sku_key(name: str) -> str:
    name = re.sub(r"\([^)]*\)", "", name.casefold())
    return re.sub(r"[^a-z0-9]", "", name)


def match_sku(name: str, skus: list[str]) -> tuple[str | None, str | None]:
    keys = {_sku_key(s): s for s in skus}
    key = _sku_key(name)
    if key in keys:
        return keys[key], None
    hit = process.extractOne(key, list(keys), score_cutoff=90)
    return (None, keys[hit[0]]) if hit else (None, None)


def normalize_currency(s: str | None) -> tuple[str, bool]:
    if s is None or not s.strip():
        return "USD", True
    c = s.strip().upper()
    if c in ("$", "USD"):
        return "USD", False
    if c in ("€", "EUR"):
        return "EUR", False
    return c, False


def _text(f: ExtractedField | None) -> str | None:
    if f is None or f.value is None or not f.value.strip():
        return None
    return f.value.strip()


def to_invoice(ext: ExtractedInvoice, skus: list[str]) -> tuple[Invoice, list[Uncertainty]]:
    unc: list[Uncertainty] = []

    def note(path: str, f: ExtractedField, normalized, parsed_ok: bool = True) -> None:
        norm = None if normalized is None else str(normalized)
        if not parsed_ok:
            unc.append(Uncertainty(field_path=path, raw=f.raw, normalized=None,
                                   confidence=f.confidence, reason="could not parse"))
        elif f.confidence != "high":
            unc.append(Uncertainty(field_path=path, raw=f.raw, normalized=norm,
                                   confidence=f.confidence,
                                   reason=f"extractor confidence {f.confidence}"))

    def conv(path: str, f: ExtractedField | None, fn):
        if f is None or f.value is None or not f.value.strip():
            return None
        v = fn(f.value)
        note(path, f, v, parsed_ok=v is not None)
        return v

    def text(path: str, f: ExtractedField | None) -> str | None:
        v = _text(f)
        if v is not None:
            note(path, f, v)
        return v

    if ext.currency is not None and _text(ext.currency) is not None:
        currency, _ = normalize_currency(ext.currency.value)
        note("currency", ext.currency, currency)
    else:
        currency = "USD"
        unc.append(Uncertainty(field_path="currency", raw=None, normalized="USD",
                               confidence="low", reason="currency missing, defaulted to USD"))

    items: list[LineItem] = []
    for i, it in enumerate(ext.items):
        p = f"items[{i}]"
        name = unicodedata.normalize("NFKC", (it.name.value or "")).strip()
        sku, suggestion = match_sku(name, skus)
        if it.name.confidence != "high":
            note(f"{p}.name", it.name, sku or name)
        if sku and re.sub(r"\s", "", name.casefold()) != sku.casefold():
            removed = " ".join(re.findall(r"\([^)]*\)", name)) or "".join(
                re.findall(r"[^\w\s]|_", name))
            unc.append(Uncertainty(field_path=f"{p}.name", raw=it.name.raw or name, normalized=sku,
                                   confidence="medium",
                                   reason=f"name normalized to {sku} (removed: '{removed}')"))
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

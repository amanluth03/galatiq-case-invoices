"""Plain-language formatting for money, quantities and dates, shared by validation flag messages and the review screen.
Pure helpers: they never change a value, only how it reads ($1,234.56, 15 Jan 2026)."""
from datetime import date
from decimal import Decimal, InvalidOperation


def money(value, currency: str = "USD") -> str:
    """Format an amount as $1,234.56 (or "1,234.56 EUR" for other currencies); "n/a" when unknown."""
    if value is None or value == "":
        return "n/a"
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    sign = "−" if d < 0 else ""
    body = f"{abs(d):,.2f}"
    return f"{sign}${body}" if (currency or "USD").upper() == "USD" else f"{sign}{body} {currency.upper()}"


def quantity(value) -> str:
    """Format a quantity without trailing zeros or exponent (20, not 2E+1 or 20.00); "n/a" when unknown."""
    if value is None:
        return "n/a"
    return f"{Decimal(value).normalize():,f}"


def nice_date(value) -> str:
    """Format a date as 15 Jan 2026; "n/a" when unknown."""
    # value.day instead of %-d: the no-padding flag is not portable (it fails on Windows).
    return f"{value.day} {value.strftime('%b %Y')}" if isinstance(value, date) else "n/a"

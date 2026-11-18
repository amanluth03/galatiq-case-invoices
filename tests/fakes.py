"""Test doubles for extraction: a scripted fake Extractor and a clean-invoice builder.
Lets pipeline tests run the extract stage without calling the real LLM.
"""
from typing import NamedTuple

from invoice_agent.models import ExtractedField, ExtractedInvoice, ExtractedLineItem


class Call(NamedTuple):
    """One recorded extractor call: the text it was given and the errors fed back."""
    raw_text: str
    errors: list[str]


class ScriptedExtractor:
    """Fake Extractor that returns (or raises) scripted responses in order and records each call."""

    def __init__(self, responses: list[ExtractedInvoice | Exception]):
        """Store the scripted responses and start with no recorded calls."""
        self.responses = list(responses)
        self.calls: list[Call] = []

    def __call__(self, raw_text: str, errors: list[str]) -> ExtractedInvoice:
        """Record the call, then return the next scripted response or raise it if it is an exception."""
        self.calls.append(Call(raw_text, list(errors)))
        # Fail loudly if the code under test calls more often than the test planned for.
        if len(self.calls) > len(self.responses):
            raise AssertionError("ScriptedExtractor called more times than scripted")
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def _f(value: str | None, raw: str | None = None) -> ExtractedField:
    """High-confidence field; raw defaults to the value."""
    return ExtractedField(value=value, raw=raw if raw is not None else value, confidence="high")


def make_extracted(**overrides) -> ExtractedInvoice:
    """Build a clean INV-1001 extraction; overrides replace top-level fields (a plain string becomes a high-confidence field)."""
    fields = {
        "invoice_number": _f("INV-1001", "Invoice #: INV-1001"),
        "vendor": _f("Widgets Inc.", "From: Widgets Inc."),
        "date": _f("2026-01-15", "Date: 2026-01-15"),
        "due_date": _f("2026-02-01", "Due: 2026-02-01"),
        "currency": _f("USD", "USD"),
        "subtotal": _f("5000.00", "Subtotal: $5,000.00"),
        "tax": _f("0.00", "Tax: $0.00"),
        "total": _f("5000.00", "Total: $5,000.00"),
        "payment_terms": _f("Net 15", "Terms: Net 15"),
        "items": [
            ExtractedLineItem(
                name=_f("WidgetA", "WidgetA"),
                qty=_f("10", "10"),
                unit_price=_f("250.00", "$250.00"),
                line_total=_f("2500.00", "$2,500.00"),
            ),
            ExtractedLineItem(
                name=_f("WidgetB", "WidgetB"),
                qty=_f("5", "5"),
                unit_price=_f("500.00", "$500.00"),
                line_total=_f("2500.00", "$2,500.00"),
            ),
        ],
    }
    for key, value in overrides.items():
        # Shorthand: a bare string becomes a high-confidence field.
        fields[key] = _f(value) if isinstance(value, str) else value
    return ExtractedInvoice(**fields)

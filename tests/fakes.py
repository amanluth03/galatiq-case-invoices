"""Test doubles: scripted fake Extractor, Analyst, VP and chat model, plus invoice, recommendation and review builders.
Lets pipeline tests run the extract and approval stages without calling the real LLM.
"""
from typing import NamedTuple

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from invoice_agent.models import (
    ExtractedField, ExtractedInvoice, ExtractedLineItem, Recommendation, VPReview,
)


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


class ScriptedAnalyst:
    """Fake AnalystFn returning (or raising) scripted Recommendations in order and recording (context, rounds) per call."""

    def __init__(self, responses: list[Recommendation | Exception]):
        """Store the scripted recommendations and start with no recorded calls."""
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, context, rounds):
        """Record the call (copying rounds), then return the next scripted recommendation or raise it if it is an exception."""
        self.calls.append((context, list(rounds)))
        if len(self.calls) > len(self.responses):
            raise AssertionError("ScriptedAnalyst called more times than scripted")
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedVP:
    """Fake VPFn returning scripted VPReviews in order and recording (context, recommendation, rounds) per call."""

    def __init__(self, responses: list[VPReview]):
        """Store the scripted reviews and start with no recorded calls."""
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, context, recommendation, rounds):
        """Record the call (copying rounds), then return the next scripted review."""
        self.calls.append((context, recommendation, list(rounds)))
        if len(self.calls) > len(self.responses):
            raise AssertionError("ScriptedVP called more times than scripted")
        return self.responses[len(self.calls) - 1]


def make_recommendation(decision="APPROVE", checklist=None, **kw) -> Recommendation:
    """Build a Recommendation with defaults; checklist=None means none given."""
    kw.setdefault("reasoning", "because")
    return Recommendation(decision=decision, checklist=checklist, **kw)


def make_review(verdict="ACCEPT", final_decision="APPROVE", feedback=None, **kw) -> VPReview:
    """Build a VPReview; for SEND_BACK pass final_decision=None."""
    kw.setdefault("reasoning", "vp reasoning")
    return VPReview(verdict=verdict, final_decision=final_decision, feedback=feedback, **kw)


class ToolCallingFakeModel(GenericFakeChatModel):
    """Fake chat model for create_agent: replies with scripted AIMessages, records the tools bound and the prompts seen."""
    bound_tools: list = []
    prompts: list = []

    def bind_tools(self, tools, **kwargs):
        """Record the tools the agent offers (names only) and keep replying from the script."""
        self.bound_tools = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Record the messages this call received, then return the next scripted reply."""
        self.prompts.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def structured_reply(schema_name: str, args: dict) -> AIMessage:
    """An AIMessage calling the structured-output tool, which is how ToolStrategy returns the final answer."""
    return AIMessage(content="", tool_calls=[{"name": schema_name, "args": args, "id": "call-1", "type": "tool_call"}])

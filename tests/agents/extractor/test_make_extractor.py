"""Tests for make_extractor: how the Grok extractor is prompted, including the self-correction retry message."""
from invoice_agent.agents.extractor.agent import EXTRACTION_PROMPT, make_extractor
from invoice_agent.models import ExtractedInvoice
from tests.fakes import make_extracted


class StructuredStub:
    """Stand-in for model.with_structured_output(...): records each message list and returns a fixed extraction."""

    def __init__(self):
        """Start with no recorded calls."""
        self.calls: list[list] = []

    def invoke(self, messages):
        """Record the messages and return a clean extraction."""
        self.calls.append(messages)
        return make_extracted()


class ModelStub:
    """Stand-in chat model that records which schema the extractor asked for."""

    def __init__(self):
        """Create the structured runnable this stub hands out."""
        self.structured = StructuredStub()
        self.schemas: list = []

    def with_structured_output(self, schema):
        """Record the schema and return the recording runnable."""
        self.schemas.append(schema)
        return self.structured


def test_first_attempt_sends_prompt_and_raw_text():
    """The first attempt sends the extraction prompt as system message and the raw text as the user message."""
    model = ModelStub()
    out = make_extractor(model)("RAW INVOICE", [])
    assert model.schemas == [ExtractedInvoice]
    assert out.invoice_number.value == "INV-1001"
    assert len(model.structured.calls) == 1
    assert model.structured.calls[0] == [("system", EXTRACTION_PROMPT), ("user", "RAW INVOICE")]


def test_retry_appends_previous_errors_as_user_message():
    """A retry adds one extra user message listing the previous attempt's errors."""
    model = ModelStub()
    make_extractor(model)("RAW", ["total: expected 5000.00, found 5500.00", "items[0]: bad qty"])
    assert len(model.structured.calls) == 1
    msgs = model.structured.calls[0]
    assert len(msgs) == 3 and msgs[2][0] == "user"
    assert "- total: expected 5000.00, found 5500.00\n- items[0]: bad qty" in msgs[2][1]

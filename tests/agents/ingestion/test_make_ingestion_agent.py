"""Tests for make_ingestion_agent: how the Grok ingestion agent is prompted, including the self-correction retry message."""
from invoice_agent.agents.ingestion.agent import INGESTION_PROMPT, make_ingestion_agent
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
    """Stand-in chat model that records which schema the ingestion agent asked for."""

    def __init__(self):
        """Create the structured runnable this stub hands out."""
        self.structured = StructuredStub()
        self.schemas: list = []
        self.kwargs: list[dict] = []

    def with_structured_output(self, schema, **kwargs):
        """Record the schema and options, and return the recording runnable."""
        self.schemas.append(schema)
        self.kwargs.append(kwargs)
        return self.structured


def test_first_attempt_sends_prompt_and_raw_text():
    """The first attempt sends the extraction prompt as system message and the raw text as the user message."""
    model = ModelStub()
    out = make_ingestion_agent(model)("RAW INVOICE", [])
    assert model.schemas == [ExtractedInvoice]
    assert out.invoice_number.value == "INV-1001"
    assert len(model.structured.calls) == 1
    assert model.structured.calls[0] == [("system", INGESTION_PROMPT), ("user", "RAW INVOICE")]


def test_retry_appends_previous_errors_as_user_message():
    """A retry adds one extra user message listing the previous attempt's errors."""
    model = ModelStub()
    make_ingestion_agent(model)("RAW", ["total: expected 5000.00, found 5500.00", "items[0]: bad qty"])
    assert len(model.structured.calls) == 1
    msgs = model.structured.calls[0]
    assert len(msgs) == 3 and msgs[2][0] == "user"
    assert "- total: expected 5000.00, found 5500.00\n- items[0]: bad qty" in msgs[2][1]


def test_uses_json_schema_structured_output():
    """The agent asks for json_schema output: in live runs Grok's function-calling mode sometimes returned empty args."""
    model = ModelStub()
    make_ingestion_agent(model)
    assert model.kwargs == [{"method": "json_schema"}]

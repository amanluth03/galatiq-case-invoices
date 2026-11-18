"""Tests for make_model: the single place the Grok chat model is built from settings."""
import pytest
from langchain_xai import ChatXAI

from invoice_agent.agents.llm import MissingApiKey, make_model
from invoice_agent.infra.config import Settings


def test_missing_key_raises_clear_error():
    """Without XAI_API_KEY, make_model fails with a message telling the user what to do."""
    with pytest.raises(MissingApiKey, match="XAI_API_KEY is not set: add it to .env"):
        make_model(Settings(xai_api_key=None))


def test_builds_chatxai_with_configured_model():
    """With a key, make_model returns a ChatXAI for the configured model, with a request timeout so calls cannot hang."""
    model = make_model(Settings(xai_api_key="test-key", xai_model="grok-test"))
    assert isinstance(model, ChatXAI) and model.model_name == "grok-test"
    assert model.request_timeout == 60 and model.max_retries == 2

"""Builds the xAI Grok chat model that every agent (ingestion agent, analyst, VP) runs on."""
from langchain_xai import ChatXAI

from invoice_agent.database.config import Settings


class MissingApiKey(RuntimeError):
    """Raised when no XAI_API_KEY is configured, so no agent can call Grok."""


def make_model(settings: Settings) -> ChatXAI:
    """Return a ChatXAI for the configured model, or raise MissingApiKey if there is no key."""
    if not settings.xai_api_key:
        raise MissingApiKey("XAI_API_KEY is not set: add it to .env")
    # temperature 0: invoice decisions should be as repeatable as the model allows.
    # timeout/max_retries: a stuck API call must fail (-> ERROR run) instead of hanging the pipeline.
    return ChatXAI(model=settings.xai_model, api_key=settings.xai_api_key, temperature=0,
                   timeout=60, max_retries=2)

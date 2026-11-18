"""Live test against the real xAI API (marked live, skipped without a key): Grok extracts the messy invoice 1002.
Run with: .venv/bin/pytest -m live -v
"""
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_agent.agents.ingestion.agent import make_ingestion_agent, run_extraction
from invoice_agent.agents.llm import make_model
from invoice_agent.infra.config import load_settings
from invoice_agent.infra.db import SEED_INVENTORY
from invoice_agent.utils.reader import read_file

ROOT = Path(__file__).parent.parent.parent


@pytest.mark.live
def test_grok_extracts_1002():
    """Grok reads 1002's abbreviated labels ("Vndr", "Inv #", "Itms") into INV-1002 with 20 GadgetX."""
    # The live test is the one place that reads the user's real .env, because it needs the key.
    settings = load_settings(ROOT / ".env")
    if not settings.xai_api_key:
        pytest.skip("XAI_API_KEY not set")
    raw = read_file(ROOT / "data" / "invoices" / "invoice_1002.txt").raw_text
    res = run_extraction(raw, make_ingestion_agent(make_model(settings)), [sku for sku, _, _ in SEED_INVENTORY])
    assert res.invoice.invoice_number == "INV-1002"
    gadget = [it for it in res.invoice.items if it.sku == "GadgetX"]
    assert len(gadget) == 1 and gadget[0].qty == Decimal("20")

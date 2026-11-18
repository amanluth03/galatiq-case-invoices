"""Live end-to-end test (marked live, skipped without a key): every sample invoice through the real Grok pipeline.
Asserts only the outcomes code guarantees (rulebook, guardrail, dedupe); LLM judgment calls are printed, not asserted.
The folder runs as a batch (BATCH_WORKERS at a time, default 5), so which copy of a pair counts as "first" can vary.
Run with: timeout 900 .venv/bin/pytest -m live tests/test_expected_outcomes.py -v -s
"""
import time
from pathlib import Path

import pytest

from invoice_agent.infra.config import Settings, load_settings
from invoice_agent.runner import build_runtime, process_dir

ROOT = Path(__file__).parent.parent
INVOICES = ROOT / "data" / "invoices"

# Each of these must be REJECTED and carry at least one of the listed must_reject flag codes.
MUST_REJECT = {
    "invoice_1002.txt": {"exceeds_stock"},
    "invoice_1005.json": {"exceeds_stock"},
    "invoice_1007.csv": {"exceeds_stock"},
    "invoice_1003.txt": {"zero_stock_item"},
    "invoice_1008.txt": {"unknown_item"},
    "invoice_1016.json": {"unknown_item"},
    "invoice_1009.json": {"non_positive_quantity", "missing_vendor"},
}

# The same invoice in two formats: exactly one copy is processed, the other is DUPLICATE (either may come first).
SAME_CONTENT_PAIRS = [("invoice_1011.pdf", "invoice_1011.txt"), ("invoice_1012.pdf", "invoice_1012.txt")]

# One invoice number with different contents: exactly one copy is REJECTED as invoice_number_reused.
REUSED_NUMBER_PAIRS = [("invoice_1004.json", "invoice_1004_revised.json")]

# 1013 as JSON and PDF: Grok sometimes extracts both identically (second copy DUPLICATE), sometimes slightly
# differently (second copy REJECTED as invoice_number_reused). Either way it is caught exactly once, and the
# original is REJECTED for ordering too many WidgetA.
BULK_1013 = ("invoice_1013.json", "invoice_1013.pdf")

# Prices differ from Acme's reference (1010 rush $300, 1014 EUR): agents alone may never pay these.
NOT_AUTO_PAID = {"invoice_1010.txt", "invoice_1014.xml"}

# Clean-by-rulebook invoices: no blocking flag; PAID or NEEDS_HUMAN_REVIEW are both acceptable judgments.
CLEAN = {"invoice_1001.txt", "invoice_1006.csv", "invoice_1015.csv"}

ALL_FILES = (set(MUST_REJECT) | NOT_AUTO_PAID | CLEAN
             | {name for pair in [*SAME_CONTENT_PAIRS, *REUSED_NUMBER_PAIRS, BULK_1013] for name in pair})


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    """Run the whole sample folder once against real Grok in a throwaway var/ dir; map file name -> final state."""
    # The live test is the one place that reads the user's real .env, because it needs the key.
    env = load_settings(ROOT / ".env")
    if not env.xai_api_key:
        pytest.skip("XAI_API_KEY not set")
    # Temporary var_dir: never touch (or wipe) the user's real var/ databases.
    settings = Settings(xai_api_key=env.xai_api_key, xai_model=env.xai_model,
                        batch_workers=env.batch_workers, var_dir=tmp_path_factory.mktemp("var"))
    settings.var_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    batch = process_dir(INVOICES, lambda: build_runtime(settings), settings.batch_workers)
    elapsed = time.perf_counter() - started
    by_file = {Path(s["source_path"]).name: s for s in batch.states}
    # Printed with -s so every outcome (including the judgment calls) is visible in the run log.
    print(f"\n{len(batch.states)} files, {settings.batch_workers} workers, {elapsed:.0f}s wall clock, "
          f"{batch.retried} retried after a timeout")
    for name, s in by_file.items():
        final = s["final"]
        codes = sorted({f.code for f in s.get("flags", []) if f.severity == "must_reject"})
        print(f"{name:28} {final.status:20} {final.decided_by:10} must_reject={codes} | {final.reasoning[:120]}")
    return by_file


def _must_reject_codes(state) -> set[str]:
    """The codes of every must_reject flag validation raised for this run."""
    return {f.code for f in state.get("flags", []) if f.severity == "must_reject"}


@pytest.mark.live
def test_every_sample_was_processed(outcomes):
    """Every one of the 20 sample files produced a run, and none ended in ERROR (e.g. a Grok timeout)."""
    assert set(outcomes) == ALL_FILES
    errors = {n: s["final"].reasoning for n, s in outcomes.items() if s["final"].status == "ERROR"}
    assert not errors


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(MUST_REJECT))
def test_rulebook_rejects(outcomes, name):
    """Invoices that break a hard rule (stock, unknown item, bad data, reused number) end REJECTED with that flag."""
    state = outcomes[name]
    codes = _must_reject_codes(state)
    assert state["final"].status == "REJECTED", state["final"].reasoning
    assert MUST_REJECT[name] & codes, codes


@pytest.mark.live
@pytest.mark.parametrize("pair", SAME_CONTENT_PAIRS)
def test_same_invoice_twice_is_processed_once(outcomes, pair):
    """Of the same invoice in two formats, exactly one is DUPLICATE (never paid twice); the other is a clean invoice."""
    states = [outcomes[name] for name in pair]
    dups = [s for s in states if s["final"].status == "DUPLICATE"]
    assert len(dups) == 1, [s["final"].status for s in states]
    original = next(s for s in states if s is not dups[0])
    assert not _must_reject_codes(original), _must_reject_codes(original)
    assert original["final"].status in {"PAID", "NEEDS_HUMAN_REVIEW"}, original["final"].reasoning


@pytest.mark.live
@pytest.mark.parametrize("pair", REUSED_NUMBER_PAIRS)
def test_reused_number_is_rejected_once(outcomes, pair):
    """Of two invoices sharing a number with different contents, exactly one is REJECTED as invoice_number_reused."""
    states = [outcomes[name] for name in pair]
    reused = [s for s in states if "invoice_number_reused" in _must_reject_codes(s)]
    assert len(reused) == 1, [sorted(_must_reject_codes(s)) for s in states]
    assert reused[0]["final"].status == "REJECTED", reused[0]["final"].reasoning
    assert all(s["final"].status != "DUPLICATE" for s in states)


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(NOT_AUTO_PAID))
def test_price_deviation_is_not_auto_paid(outcomes, name):
    """An invoice whose prices differ from Acme's reference is rejected or held for a human, never paid by agents."""
    assert outcomes[name]["final"].status in {"REJECTED", "NEEDS_HUMAN_REVIEW"}, outcomes[name]["final"].reasoning


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(CLEAN))
def test_clean_invoice_has_no_blocking_flag(outcomes, name):
    """Clean invoices raise no must_reject flag and end PAID or NEEDS_HUMAN_REVIEW (the agents' judgment)."""
    state = outcomes[name]
    assert not _must_reject_codes(state), _must_reject_codes(state)
    assert state["final"].status in {"PAID", "NEEDS_HUMAN_REVIEW"}, state["final"].reasoning


@pytest.mark.live
def test_bulk_1013_caught_once(outcomes):
    """Of 1013's two formats exactly one is the second copy (DUPLICATE or reused number); the original is REJECTED for stock."""
    states = [outcomes[name] for name in BULK_1013]
    second = [s for s in states
              if s["final"].status == "DUPLICATE" or "invoice_number_reused" in _must_reject_codes(s)]
    assert len(second) == 1, [s["final"].status for s in states]
    original = next(s for s in states if s is not second[0])
    assert original["final"].status == "REJECTED", original["final"].reasoning
    assert "exceeds_stock" in _must_reject_codes(original), _must_reject_codes(original)

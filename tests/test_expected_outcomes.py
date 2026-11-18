"""Live end-to-end test (marked live, skipped without a key): every sample invoice through the real Grok pipeline.
Asserts only the outcomes code guarantees (rulebook, guardrail, dedupe); LLM judgment calls are printed, not asserted.
Run with: timeout 1500 .venv/bin/pytest -m live tests/test_expected_outcomes.py -v -s
"""
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
    # First-sorted copy of 1013: too many WidgetA in total, and the stated total is $50 over its lines.
    "invoice_1013.json": {"exceeds_stock", "total_exceeds_line_items"},
    # Sorted after invoice_1004.json, so it reuses an already-processed number with different contents.
    "invoice_1004_revised.json": {"invoice_number_reused"},
}
# invoice_1013.json needs BOTH codes, not just one of them.
MUST_HAVE_ALL = {"invoice_1013.json"}

# Second-sorted copy of each pdf + txt/json pair: the same invoice seen twice must be processed once.
DUPLICATES = {"invoice_1011.txt", "invoice_1012.txt", "invoice_1013.pdf"}

# Prices differ from Acme's reference (1010 rush $300, 1014 EUR): agents alone may never pay these.
NOT_AUTO_PAID = {"invoice_1010.txt", "invoice_1014.xml"}

# Clean-by-rulebook invoices: no blocking flag; PAID or NEEDS_HUMAN_REVIEW are both acceptable judgments.
CLEAN = {"invoice_1001.txt", "invoice_1004.json", "invoice_1006.csv", "invoice_1015.csv",
         "invoice_1011.pdf", "invoice_1012.pdf"}


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    """Run the whole sample folder once against real Grok in a throwaway var/ dir; map file name -> final state."""
    # The live test is the one place that reads the user's real .env, because it needs the key.
    env = load_settings(ROOT / ".env")
    if not env.xai_api_key:
        pytest.skip("XAI_API_KEY not set")
    # Temporary var_dir: never touch (or wipe) the user's real var/ databases.
    settings = Settings(xai_api_key=env.xai_api_key, xai_model=env.xai_model,
                        var_dir=tmp_path_factory.mktemp("var"))
    settings.var_dir.mkdir(parents=True, exist_ok=True)
    deps, graph = build_runtime(settings)
    states = process_dir(INVOICES, deps, graph)
    by_file = {Path(s["source_path"]).name: s for s in states}
    # Printed with -s so every outcome (including the judgment calls) is visible in the run log.
    print()
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
    expected = set(MUST_REJECT) | DUPLICATES | NOT_AUTO_PAID | CLEAN
    assert set(outcomes) == expected
    errors = {n: s["final"].reasoning for n, s in outcomes.items() if s["final"].status == "ERROR"}
    assert not errors


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(MUST_REJECT))
def test_rulebook_rejects(outcomes, name):
    """Invoices that break a hard rule (stock, unknown item, bad data, reused number) end REJECTED with that flag."""
    state = outcomes[name]
    codes = _must_reject_codes(state)
    assert state["final"].status == "REJECTED", state["final"].reasoning
    if name in MUST_HAVE_ALL:
        assert MUST_REJECT[name] <= codes, codes
    else:
        assert MUST_REJECT[name] & codes, codes


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(DUPLICATES))
def test_second_copy_is_duplicate(outcomes, name):
    """The second format of the same invoice (pdf + txt/json) is marked DUPLICATE, so it can never be paid twice."""
    assert outcomes[name]["final"].status == "DUPLICATE", outcomes[name]["final"].reasoning


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

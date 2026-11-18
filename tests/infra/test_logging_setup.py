"""Tests for the JSON-lines run log."""
import json
import logging

from invoice_agent.infra.logging_setup import RUN_LOGGER, setup_logging


def test_records_are_written_as_json_lines(tmp_path):
    """A logged node event lands in run.jsonl as one JSON object carrying its structured fields."""
    setup_logging(tmp_path / "logs")
    logging.getLogger(RUN_LOGGER).info("node_completed", extra={"fields": {"run_id": "r1", "node": "read"}})
    lines = (tmp_path / "logs" / "run.jsonl").read_text().splitlines()
    record = json.loads(lines[0])
    assert record["event"] == "node_completed" and record["run_id"] == "r1" and record["node"] == "read"


def test_setup_twice_does_not_duplicate_lines(tmp_path):
    """Calling setup_logging again replaces the handlers, so each record is written once."""
    setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")
    logging.getLogger(RUN_LOGGER).info("node_completed", extra={"fields": {"node": "read"}})
    assert len((tmp_path / "logs" / "run.jsonl").read_text().splitlines()) == 1

"""Logging setup: a JSON-lines run log (one record per graph node) plus a readable rich console for warnings.
Used by the runner and CLI around every pipeline run.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.logging import RichHandler

# The runner logs under this logger name; every handler below hangs off the "invoice_agent" parent.
RUN_LOGGER = "invoice_agent.run"


class JsonLinesFormatter(logging.Formatter):
    """Formats each record as one JSON object: timestamp, level, event name and the structured fields given via extra."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single JSON line."""
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            # `fields` is set by callers through extra={"fields": {...}} (run_id, node, duration_ms, ...).
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, default=str)


def setup_logging(log_dir: Path) -> None:
    """Send run records to log_dir/run.jsonl and warnings to a rich console; safe to call repeatedly."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("invoice_agent")
    logger.setLevel(logging.INFO)
    # Drop handlers from an earlier call so a second setup (tests, server reload) does not duplicate lines.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    file_handler = logging.FileHandler(log_dir / "run.jsonl", encoding="utf-8")
    file_handler.setFormatter(JsonLinesFormatter())
    # Console shows warnings only: the per-node records are for the file, the CLI prints its own result output.
    console = RichHandler(level=logging.WARNING, show_path=False)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    # Keep the records out of the root logger so they are not printed twice.
    logger.propagate = False

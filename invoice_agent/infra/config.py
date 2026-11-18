"""Runtime settings and tunable limits (env/.env loading). Used by every stage of the pipeline.
"""
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Business knobs: analyst/VP critique rounds, the "over $10K needs scrutiny" line, LLM re-extraction retries.
MAX_ROUNDS = 3
SCRUTINY_THRESHOLD = Decimal("10000")
MAX_EXTRACTION_RETRIES = 2


# grok-3 timed out / returned HTTP 500 on the analyst's tool-calling loop in live testing;
# this model ran the whole pipeline end to end.
DEFAULT_XAI_MODEL = "grok-4-1-fast-non-reasoning"


@dataclass
class Settings:
    """Environment-driven settings; all runtime files live under var_dir."""
    xai_api_key: str | None = None
    xai_model: str = DEFAULT_XAI_MODEL
    var_dir: Path = field(default_factory=lambda: Path("var"))

    @property
    def app_db(self) -> Path:
        """SQLite file holding inventory and run history."""
        return self.var_dir / "app.db"

    @property
    def checkpoint_db(self) -> Path:
        """SQLite file for LangGraph checkpoints (pause/resume of human review)."""
        return self.var_dir / "checkpoints.db"

    @property
    def log_dir(self) -> Path:
        """Directory for per-run log output."""
        return self.var_dir / "logs"


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Load settings from the environment, after reading only the given .env file."""
    if env_file is not None and Path(env_file).exists():
        # override=False: real environment variables win over .env values.
        load_dotenv(env_file, override=False)
    return Settings(
        xai_api_key=os.environ.get("XAI_API_KEY") or None,
        xai_model=os.environ.get("XAI_MODEL") or DEFAULT_XAI_MODEL,
    )

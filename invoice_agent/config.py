import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

MAX_ROUNDS = 3
SCRUTINY_THRESHOLD = Decimal("10000")
MAX_EXTRACTION_RETRIES = 2


@dataclass
class Settings:
    xai_api_key: str | None = None
    xai_model: str = "grok-3"
    var_dir: Path = field(default_factory=lambda: Path("var"))

    @property
    def app_db(self) -> Path:
        return self.var_dir / "app.db"

    @property
    def checkpoint_db(self) -> Path:
        return self.var_dir / "checkpoints.db"

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "logs"


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Load settings from the environment, after reading only the given .env file."""
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    return Settings(
        xai_api_key=os.environ.get("XAI_API_KEY") or None,
        xai_model=os.environ.get("XAI_MODEL") or "grok-3",
    )

"""Subprocess tests for main.py: argument checking and the missing-API-key exit code."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
MAIN = ROOT / "main.py"


def run_cli(tmp_path, *args):
    """Run main.py from an empty cwd with no XAI_API_KEY (and so no .env) and return the finished process."""
    env = {k: v for k, v in os.environ.items() if k != "XAI_API_KEY"}
    return subprocess.run([sys.executable, str(MAIN), *args], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def test_cli_missing_key_exits_2(tmp_path):
    """Without XAI_API_KEY the CLI prints the reason to stderr and exits 2."""
    result = run_cli(tmp_path, f"--invoice_path={ROOT / 'data/invoices/invoice_1001.txt'}")
    assert result.returncode == 2
    assert "XAI_API_KEY is not set" in result.stderr


def test_cli_requires_path_or_dir(tmp_path):
    """Running with no input flag is a usage error (exit 2)."""
    assert run_cli(tmp_path).returncode == 2


def test_cli_missing_file_exits_2(tmp_path):
    """A path that does not exist is a usage error (exit 2), reported before any key check."""
    assert run_cli(tmp_path, "--invoice_path=nope.txt").returncode == 2


def test_cli_serve_placeholder(tmp_path):
    """--serve prints the Task 11 placeholder and exits 0."""
    result = run_cli(tmp_path, "--serve")
    assert result.returncode == 0 and "Review screen arrives in Task 11" in result.stdout

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


def test_cli_reset_reports_and_exits_0(tmp_path):
    """--reset alone clears the databases, says so and exits 0."""
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "app.db").write_text("x")
    result = run_cli(tmp_path, "--reset")
    assert result.returncode == 0 and "Databases reset" in result.stdout
    assert not (tmp_path / "var" / "app.db").exists()


def test_cli_serve_with_reset_resets_first(tmp_path):
    """--serve --reset performs the reset before the (placeholder) serve."""
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "app.db").write_text("x")
    result = run_cli(tmp_path, "--serve", "--reset")
    assert result.returncode == 0 and "Databases reset" in result.stdout
    assert not (tmp_path / "var" / "app.db").exists()


def _state(vendor="Acme", reasoning="ok"):
    """A minimal PAID state whose vendor and reasoning are chosen by the test."""
    from invoice_agent.models import FinalOutcome, Invoice
    return {"run_id": "r", "source_path": "f.txt", "invoice": Invoice(invoice_number="1", vendor=vendor),
            "final": FinalOutcome(status="REJECTED", decided_by="agents", reasoning=reasoning)}


def _captured(monkeypatch, show, arg):
    """Run a main.py print function against a plain-text console and return what it printed."""
    import io
    sys.path.insert(0, str(ROOT))
    import main
    from rich.console import Console
    buf = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=buf, width=200, force_terminal=False))
    getattr(main, show)(arg)
    return buf.getvalue()


def test_markup_in_vendor_renders_literally(monkeypatch):
    """A vendor named "[green]PAID[/green]" is printed as-is in both the panel and the table."""
    state = _state(vendor="[green]PAID[/green]")
    assert "[green]PAID[/green]" in _captured(monkeypatch, "show_panel", state)
    assert "[green]PAID[/green]" in _captured(monkeypatch, "show_table", [state])


def test_stray_closing_tag_in_reasoning_does_not_raise(monkeypatch):
    """Reasoning containing a stray "[/x]" tag is printed literally instead of raising MarkupError."""
    state = _state(reasoning="[/x] oops\nsecond line")
    assert "[/x] oops second line" in _captured(monkeypatch, "show_panel", state)
    assert "[/x] oops" in _captured(monkeypatch, "show_table", [state])

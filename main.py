"""Command-line entry point: process one invoice or a directory and print the outcome, or start the review screen (--serve).
Exit codes: 0 ok, 2 bad arguments or missing XAI_API_KEY.
"""
import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from invoice_agent.agents.llm import MissingApiKey
from invoice_agent.infra import db
from invoice_agent.infra.config import load_settings
from invoice_agent.infra.logging_setup import setup_logging
from invoice_agent.models import PipelineState
from invoice_agent.runner import build_runtime, close_runtime, process_dir, process_file

console = Console()


def _amount(state: PipelineState) -> str:
    """The invoice total as text, or a dash when it could not be read."""
    inv = state.get("invoice")
    return str(inv.total) if inv is not None and inv.total is not None else "-"


def _invoice_no(state: PipelineState) -> str:
    """The invoice number, falling back to the file name when extraction produced none."""
    inv = state.get("invoice")
    number = (inv.invoice_number if inv is not None and inv.invoice_number else None) or Path(state["source_path"]).name
    return _one_line(number, 40)


def _vendor(state: PipelineState) -> str:
    """The vendor name, or a dash when unknown."""
    inv = state.get("invoice")
    return _one_line((inv.vendor if inv is not None and inv.vendor else None) or "-", 60)


def _one_line(text: str, width: int = 90) -> str:
    """Collapse a reasoning text to one line, cut to width."""
    line = " ".join(text.split())
    return line if len(line) <= width else line[: width - 1] + "…"


def show_panel(state: PipelineState) -> None:
    """Print one run as a panel: invoice no., vendor, amount, outcome, why, decided by."""
    final = state["final"]
    # Text (not a str) everywhere: vendor/LLM-controlled values must never be parsed as rich markup.
    body = Text()
    for label, value in (("Invoice", _invoice_no(state)), ("Vendor", _vendor(state)), ("Amount", _amount(state)),
                         ("Outcome", final.status), ("Why", _one_line(final.reasoning, 300)),
                         ("Decided by", final.decided_by)):
        body.append(f"{label + ':':<12}")
        body.append(value + "\n" if label != "Decided by" else value)
    console.print(Panel(body, title=Text(Path(state["source_path"]).name)))


def show_table(states: list[PipelineState]) -> None:
    """Print a batch as a table, one row per invoice."""
    table = Table("Invoice", "Vendor", "Amount", "Outcome", "Why")
    for s in states:
        table.add_row(*(Text(c) for c in (_invoice_no(s), _vendor(s), _amount(s), s["final"].status,
                                          _one_line(s["final"].reasoning))))
    console.print(table)


def serve(port: int) -> int:
    """Start the review screen on localhost with the real runtime; returns the exit code (2 without an API key)."""
    # Imported here so plain CLI runs don't pay for loading the web stack.
    import uvicorn

    from invoice_agent.web.app import create_app

    settings = load_settings()
    try:
        deps, graph = build_runtime(settings)
    except MissingApiKey as exc:
        # Resuming a review runs the rest of the graph, so the server needs the same key as the CLI.
        print(str(exc), file=sys.stderr)
        return 2
    setup_logging(settings.log_dir)
    console.print(f"Review screen → http://localhost:{port}")
    # settings: uploads and every background job's own runtime use the same var/ files as the server.
    uvicorn.run(create_app(deps, graph, deps.conn, settings=settings), host="127.0.0.1", port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the requested files and print results; returns the exit code."""
    parser = argparse.ArgumentParser(description="Process invoices through the approval pipeline.")
    parser.add_argument("--invoice_path", help="a single invoice file")
    parser.add_argument("--invoice_dir", help="a directory of invoice files")
    parser.add_argument("--reset", action="store_true", help="delete the databases before running")
    parser.add_argument("--serve", action="store_true", help="start the review screen")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)  # argparse itself exits 2 on bad flags

    if args.serve:
        if args.reset:
            # Reset first so the server starts on clean databases.
            db.reset(load_settings())
            print("Databases reset")
        return serve(args.port)
    if not args.invoice_path and not args.invoice_dir and not args.reset:
        parser.error("give --invoice_path or --invoice_dir")
    if args.invoice_path and not Path(args.invoice_path).is_file():
        parser.error(f"no such file: {args.invoice_path}")
    if args.invoice_dir and not Path(args.invoice_dir).is_dir():
        parser.error(f"no such directory: {args.invoice_dir}")

    settings = load_settings()
    if args.reset:
        db.reset(settings)
        print("Databases reset")
    if not args.invoice_path and not args.invoice_dir:
        return 0
    try:
        deps, graph = build_runtime(settings)
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    setup_logging(settings.log_dir)

    if args.invoice_path:
        states = [process_file(Path(args.invoice_path), deps, graph)]
        show_panel(states[0])
    else:
        # The runtime above only proved the key works: each batch worker builds its own (SQLite is per thread).
        close_runtime(deps, graph)
        batch = process_dir(Path(args.invoice_dir), lambda: build_runtime(settings), settings.batch_workers)
        states = batch.states
        show_table(states)
        if batch.retried:
            console.print(f"{batch.retried} run(s) timed out on Grok and were retried once at the end")
    paused = sum(1 for s in states if s["final"].status == "NEEDS_HUMAN_REVIEW")
    if paused:
        console.print(f"{paused} need human review → open http://localhost:{args.port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

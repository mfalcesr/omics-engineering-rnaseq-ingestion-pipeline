"""Command-line entry point.

    ingest load-payload <file.json>      # ingest one payload
    ingest load-dir <directory>          # ingest every *.json in a directory (sorted)

Exit code is non-zero if any payload was rejected, so orchestrators and CI can detect failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
import typer

from .config import load_settings
from .load import IngestOutcome, ingest_payload
from .logging_conf import configure_logging, get_logger

app = typer.Typer(add_completion=False, help="RNA-seq vendor payload ingestion.")


def _connect(settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def _ingest_files(files: list[Path], *, json_logs: bool) -> int:
    """Ingest a list of files; return a process exit code."""
    configure_logging(json_output=json_logs)
    logger = get_logger()
    settings = load_settings()

    outcomes: list[IngestOutcome] = []
    with _connect(settings) as conn:
        for path in files:
            raw = json.loads(path.read_text(encoding="utf-8"))
            outcome = ingest_payload(conn, raw, actor=settings.actor, logger=logger)
            outcomes.append(outcome)
            _print_outcome(path, outcome)

    rejected = [o for o in outcomes if o.status == "rejected"]
    loaded = [o for o in outcomes if o.status == "loaded"]
    skipped = [o for o in outcomes if o.status == "skipped"]
    typer.echo(
        f"\nSummary: {len(loaded)} loaded, {len(skipped)} skipped, "
        f"{len(rejected)} rejected ({len(outcomes)} payload(s))."
    )
    return 1 if rejected else 0


def _print_outcome(path: Path, outcome: IngestOutcome) -> None:
    # ASCII-only markers so output never crashes on a non-UTF-8 Windows console.
    if outcome.status == "rejected":
        typer.echo(f"\n[REJECTED] {path.name}", err=True)
        # The structured, machine-readable rejection document.
        typer.echo(json.dumps(outcome.report, indent=2, default=str), err=True)
    else:
        detail = outcome.reason or ""
        if outcome.diff:
            detail += f" diff={json.dumps(outcome.diff, default=str)}"
        typer.echo(
            f"[{outcome.status.upper():9}] {path.name} "
            f"(sample_id={outcome.sample_id}, expr_rows={outcome.expression_rows}) {detail}"
        )


@app.command("load-payload")
def load_payload(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    json_logs: bool = typer.Option(True, "--json-logs/--console", help="JSON vs console logs."),
) -> None:
    """Ingest a single vendor payload JSON file."""
    code = _ingest_files([file], json_logs=json_logs)
    raise typer.Exit(code)


@app.command("load-dir")
def load_dir(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, readable=True),
    json_logs: bool = typer.Option(True, "--json-logs/--console", help="JSON vs console logs."),
) -> None:
    """Ingest every ``*.json`` payload in a directory (sorted by filename)."""
    files = sorted(directory.glob("*.json"))
    if not files:
        typer.echo(f"No *.json files found in {directory}", err=True)
        raise typer.Exit(2)
    code = _ingest_files(files, json_logs=json_logs)
    raise typer.Exit(code)


def main() -> None:  # pragma: no cover - console-script shim
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())  # type: ignore[func-returns-value]

"""Typer-based ``homing`` CLI.

This module wires the eight phase commands. Most are stubs at this stage; only
``summary`` is fully implemented (Phase B), since it has no LLM dependency and
serves as the first usable artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from homing import __version__
from homing import summary as summary_module

app = typer.Typer(
    name="homing",
    help=(
        "Make a personal laptop legible to agents. Walks $HOME and writes "
        "a structured representation under ~/system/."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_console = Console()
_DEFAULT_SYSTEM_DIR = Path.home() / "system"
_DEFAULT_HOME = Path.home()


def _stub(name: str) -> None:
    typer.echo(f"[homing {name}] not yet implemented")
    raise typer.Exit(code=1)


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the homing version and exit.",
        is_eager=True,
    ),
) -> None:
    if version:
        typer.echo(f"homing {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Phase A — enumerate (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase A: deterministic two-pass walk of $HOME (stub).")
def enumerate(
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to platform config YAML override."
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="Output directory for enumeration.json (defaults to ~/system)."
    ),
) -> None:
    del config, output
    _stub("enumerate")


# ---------------------------------------------------------------------------
# Phase B — summary (implemented)
# ---------------------------------------------------------------------------


@app.command(help="Phase B: deterministic 5-minute overview of $HOME, no LLM.")
def summary(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="Output directory. Defaults to ~/system.",
    ),
    home: Path = typer.Option(
        _DEFAULT_HOME,
        "--home",
        help="Source directory to summarize. Defaults to $HOME.",
    ),
) -> None:
    out_path = summary_module.run(home=home, system_dir=system_dir)
    _console.print(f"[green]wrote[/] {out_path}")


# ---------------------------------------------------------------------------
# Phase C — rules (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase C: run deterministic rule plugins over enumerated units (stub).")
def rules() -> None:
    _stub("rules")


# ---------------------------------------------------------------------------
# Phase D — classify (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase D: LLM classification for low-confidence units (stub).")
def classify() -> None:
    _stub("classify")


# ---------------------------------------------------------------------------
# Phase E — draft (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase E: draft AGENT.md / PLACE.md for a unit (stub).")
def draft(name: str = typer.Argument(..., help="Unit name.")) -> None:
    del name
    _stub("draft")


# ---------------------------------------------------------------------------
# Phase F — validate (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase F: fresh-agent validation of a manifest (stub).")
def validate(name: str = typer.Argument(..., help="Unit name.")) -> None:
    del name
    _stub("validate")


# ---------------------------------------------------------------------------
# Phase G — index (stub)
# ---------------------------------------------------------------------------


@app.command(help="Phase G: aggregate manifests into index.json (stub).")
def index() -> None:
    _stub("index")


# ---------------------------------------------------------------------------
# query (stub group)
# ---------------------------------------------------------------------------


query_app = typer.Typer(help="Query the aggregated manifest index (stub).")
app.add_typer(query_app, name="query")


@query_app.command("list", help="List units (stub).")
def query_list() -> None:
    _stub("query list")


@query_app.command("show", help="Show a single unit (stub).")
def query_show(name: str = typer.Argument(..., help="Unit name.")) -> None:
    del name
    _stub("query show")


@query_app.command("stale", help="List stale units (stub).")
def query_stale() -> None:
    _stub("query stale")


@query_app.command("active", help="List active units (stub).")
def query_active() -> None:
    _stub("query active")


# ---------------------------------------------------------------------------
# reconcile (stub)
# ---------------------------------------------------------------------------


@app.command(help="Reconcile a *.proposed.md manifest with an existing one (stub).")
def reconcile(name: str = typer.Argument(..., help="Unit name.")) -> None:
    del name
    _stub("reconcile")


if __name__ == "__main__":  # pragma: no cover
    app()

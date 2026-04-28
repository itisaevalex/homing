"""CLI shim for ``homing draft``.

This file is intentionally separate from :mod:`homing.cli` so that the
drafter can be developed and integrated without colliding with the
phase-orchestration work happening in ``cli.py``. The integration step is
trivial: in ``cli.py`` either::

    from homing.draft_cli import draft_app
    app.add_typer(draft_app, name="draft")

…or import :func:`register_draft_command` and call it with the existing
``app``::

    from homing.draft_cli import register_draft_command
    register_draft_command(app)

Both paths end up with a working ``homing draft <name>`` command. The
second is preferred because it matches the existing pattern in ``cli.py``
where each phase is a single command, not a sub-typer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from homing.draft import VALID_POLICIES, draft_agent_md
from homing.worklist import Worklist

_console = Console()
_DEFAULT_SYSTEM_DIR = Path.home() / "system"
_DEFAULT_MODEL = "claude-sonnet-4-6"


# A tiny standalone Typer app — useful for `python -m homing.draft_cli`
# style invocation and also gives the integration "add_typer" path a target.
draft_app = typer.Typer(
    name="draft",
    help="Phase E: draft an AGENT.md / PLACE.md for a unit using the LLM.",
    no_args_is_help=True,
    add_completion=False,
)


def _project_path_for(name: str, system_dir: Path) -> Path:
    """Resolve a unit name to its on-disk project path via the worklist.

    The worklist is the cross-phase source of truth; ``enumerate``
    populated it. Falls back to a clear error when the unit isn't there.
    """
    worklist_path = system_dir / "worklist.sqlite"
    if not worklist_path.is_file():
        raise typer.BadParameter(
            f"worklist not found at {worklist_path}. Run `homing enumerate` first."
        )
    with Worklist(worklist_path) as wl:
        unit = wl.unit(name)
        if unit is None:
            raise typer.BadParameter(
                f"no unit named {name!r} in {worklist_path}"
            )
        if unit["kind"] != "project":
            raise typer.BadParameter(
                f"unit {name!r} is a {unit['kind']!r}, not a project; "
                f"AGENT.md drafting only applies to projects"
            )
        return Path(unit["path"])


def _run_draft(
    name: str,
    system_dir: Path,
    model: str,
    policy: str,
    project_path_override: Optional[Path] = None,
) -> int:
    """Shared command body — returns the intended process exit code."""
    if policy not in VALID_POLICIES:
        _console.print(
            f"[red]error[/] policy must be one of {list(VALID_POLICIES)}, got {policy!r}"
        )
        return 2

    if project_path_override is not None:
        project_path = project_path_override
    else:
        try:
            project_path = _project_path_for(name, system_dir)
        except typer.BadParameter as e:
            _console.print(f"[red]error[/] {e.message}")
            return 2

    output_path = system_dir / "projects" / name / "AGENT.md"

    _console.print(
        f"[bold]drafting[/] {name} "
        f"(project={project_path}, policy={policy}, model={model})"
    )

    result = draft_agent_md(
        project_path=project_path,
        output_path=output_path,
        model=model,
        overwrite_policy=policy,
    )

    color = {
        "drafted": "green",
        "proposed": "yellow",
        "skipped": "blue",
        "failed": "red",
    }.get(result.status, "white")
    _console.print(f"[{color}]{result.status}[/]  {result.output_path or '(no file)'}")
    if result.reason:
        _console.print(f"  reason: {result.reason}")
    if result.input_files_used:
        _console.print(
            f"  inputs: {len(result.input_files_used)} files / "
            f"{result.tokens_input} input tok, {result.tokens_output} output tok"
        )

    if result.status in ("drafted", "proposed"):
        return 0
    if result.status == "skipped":
        return 0  # idempotent skip is not an error
    return 1


@draft_app.callback(invoke_without_command=True)
def draft(
    name: str = typer.Argument(..., help="Unit name (must be a project in the worklist)."),
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="Output root. Manifests land in <system-dir>/projects/<name>/AGENT.md.",
    ),
    model: str = typer.Option(
        _DEFAULT_MODEL,
        "--model",
        help="Anthropic model id.",
    ),
    policy: str = typer.Option(
        "proposed",
        "--policy",
        help=(
            "Conflict policy when AGENT.md already exists: "
            "'proposed' writes a sibling .proposed.md; "
            "'skip' returns success without calling the LLM; "
            "'fail' raises."
        ),
    ),
    project_path: Optional[Path] = typer.Option(
        None,
        "--project-path",
        help="Override worklist lookup. Mostly for tests / one-offs.",
    ),
) -> None:
    """Draft an AGENT.md for ``name`` using the LLM."""
    code = _run_draft(name, system_dir, model, policy, project_path)
    raise typer.Exit(code=code)


def register_draft_command(app: typer.Typer) -> None:
    """Wire ``draft`` into an existing top-level Typer app.

    Used by ``cli.py`` once the orchestrator is ready to swap its stub
    for the real implementation. Replaces any pre-existing ``draft``
    command on ``app`` because Typer commands are name-keyed — the
    integrating side should remove its stub before calling this.
    """

    @app.command(
        name="draft",
        help="Phase E: draft an AGENT.md / PLACE.md for a unit using the LLM.",
    )
    def _draft(
        name: str = typer.Argument(..., help="Unit name."),
        system_dir: Path = typer.Option(
            _DEFAULT_SYSTEM_DIR, "--system-dir"
        ),
        model: str = typer.Option(_DEFAULT_MODEL, "--model"),
        policy: str = typer.Option("proposed", "--policy"),
        project_path: Optional[Path] = typer.Option(None, "--project-path"),
    ) -> None:
        code = _run_draft(name, system_dir, model, policy, project_path)
        raise typer.Exit(code=code)


if __name__ == "__main__":  # pragma: no cover
    draft_app()

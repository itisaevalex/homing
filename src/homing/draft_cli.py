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
        via_orchestrator: bool = typer.Option(
            False,
            "--via-orchestrator",
            help=(
                "Defer the LLM call to the orchestrating Claude Code session. "
                "Writes a draft request bundle (collected inputs + schema + "
                "target path + conflict policy) to <system-dir>/draft-requests/<name>.json. "
                "The orchestrator (you) reads it, runs a sub-agent that drafts the AGENT.md "
                "and writes it to the target path. No ANTHROPIC_API_KEY needed."
            ),
        ),
    ) -> None:
        if via_orchestrator:
            code = _run_draft_via_orchestrator(
                name, system_dir, project_path, policy, model
            )
        else:
            code = _run_draft(name, system_dir, model, policy, project_path)
        raise typer.Exit(code=code)


def _run_draft_via_orchestrator(
    name: str,
    system_dir: Path,
    project_path: Optional[Path],
    policy: str,
    model: str = _DEFAULT_MODEL,
) -> int:
    """Emit a draft request bundle for the orchestrator to handle, then exit."""
    import json
    from homing import draft as _draft_mod

    system_dir = system_dir.expanduser().resolve()

    # Resolve project path (worklist lookup or explicit override). The lookup
    # raises ``typer.BadParameter`` (not ``FileNotFoundError``) — catch the
    # actual exception type so we don't leak a raw traceback to the user.
    if project_path is None:
        try:
            project_path = _project_path_for(name, system_dir)
        except typer.BadParameter as exc:
            _console.print(f"[red]error:[/red] {exc.message}")
            return 2
        except (FileNotFoundError, OSError) as exc:
            _console.print(f"[red]error:[/red] {exc}")
            return 2
    project_path = project_path.expanduser().resolve()
    if not project_path.is_dir():
        _console.print(f"[red]error:[/red] project path not found: {project_path}")
        return 2

    target_path = system_dir / "projects" / name / "AGENT.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and policy == "proposed":
        target_path = target_path.with_name("AGENT.proposed.md")
    elif target_path.exists() and policy == "skip":
        _console.print(f"[yellow]skipped:[/yellow] {target_path} already exists (policy=skip)")
        return 0
    elif target_path.exists() and policy == "fail":
        _console.print(f"[red]error:[/red] {target_path} exists and policy=fail")
        return 2

    # Collect the same input bundle that draft.py would have sent to the LLM
    try:
        inputs = _draft_mod.collect_inputs(project_path)
    except Exception as exc:
        _console.print(f"[red]error collecting inputs:[/red] {exc}")
        return 2

    try:
        schema_text = _draft_mod._load_schema_text()
    except Exception as exc:
        _console.print(f"[red]error loading schema:[/red] {exc}")
        return 2
    try:
        user_msg = _draft_mod._build_user_message(project_path, inputs)
    except Exception as exc:
        _console.print(f"[red]error building user message:[/red] {exc}")
        return 2

    requests_dir = system_dir / "draft-requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    request_path = requests_dir / f"{name}.json"
    request_path.write_text(
        json.dumps(
            {
                "name": name,
                "project_path": str(project_path),
                "target_path": str(target_path),
                "schema": schema_text,
                "user_message": user_msg,
                "input_files": [str(f.path) for f in inputs],
                "policy": policy,
                "model": model,
                "instructions": (
                    "You are the orchestrator. Spawn a sub-agent and pass it the user_message + schema. "
                    "The sub-agent should produce a complete AGENT.md (frontmatter + body) following the "
                    "schema, citing the listed input_files in meta.sources. Write the result to target_path. "
                    "Do NOT overwrite target_path if it exists — the policy already resolved this. "
                    "The 'model' field is the suggested model for this draft; the orchestrator may "
                    "pick a different one based on its session config."
                ),
            },
            indent=2,
        )
    )

    _console.print(
        f"[cyan]draft --via-orchestrator:[/cyan] request written to {request_path}\n"
        f"[cyan]Target:[/cyan] {target_path}\n"
        f"[cyan]Next:[/cyan] orchestrator reads the request, fires a sub-agent, writes AGENT.md to target."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    draft_app()

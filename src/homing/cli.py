"""Typer-based ``homing`` CLI.

Wires the eight phase commands onto the modules that implement them. Phases
A (enumerate), B (summary), C (rules), and G (index) are operational; the
intervening LLM-touching phases are still stubs and exit with code 1 when
invoked, so users see a clear error rather than silent no-ops.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from homing import __version__
from homing import enumerate as enumerate_module
from homing import index as index_module
from homing import orchestrator as orchestrator_module
from homing import platform as platform_module
from homing import summary as summary_module
from homing import validate as validate_module
from homing.worklist import Worklist
from homing.draft_cli import register_draft_command

app = typer.Typer(
    name="homing",
    help=(
        "Make a personal laptop legible to agents. Walks $HOME and writes "
        "a structured representation under ~/system/."
    ),
    no_args_is_help=True,
    add_completion=False,
    invoke_without_command=True,
)

_console = Console()
_DEFAULT_SYSTEM_DIR = Path.home() / "system"
_DEFAULT_HOME = Path.home()
_STALE_DAYS = 90
_SECONDS_PER_DAY = 86400


def _stub(name: str) -> None:
    typer.echo(f"[homing {name}] not yet implemented")
    raise typer.Exit(code=1)


@app.callback()
def _root(
    ctx: typer.Context,
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
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Phase A — enumerate
# ---------------------------------------------------------------------------


@app.command(help="Phase A: deterministic two-pass walk of $HOME.")
def enumerate(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="Output directory. Defaults to ~/system."
    ),
    home: Path = typer.Option(
        _DEFAULT_HOME, "--home", help="Source directory to walk. Defaults to $HOME."
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Override the platform config YAML. Defaults to the bundled per-platform file.",
    ),
) -> None:
    system_dir = system_dir.resolve()
    home = home.resolve()
    system_dir.mkdir(parents=True, exist_ok=True)

    cfg = _load_config(config)

    worklist = Worklist(system_dir / "worklist.sqlite")
    run_id = worklist.start_run("enumerate")
    try:
        result = enumerate_module.enumerate_home(home, cfg)
        out_path = system_dir / "enumeration.json"
        out_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        for project in result["projects"]:
            _upsert_unit(
                worklist,
                kind="project",
                name=_unit_name_from_path(project["path"], home),
                path=project["path"],
                payload={
                    "signals_found": project.get("signals_found", []),
                    "size_bytes": project.get("size_bytes", 0),
                    "last_mtime": project.get("last_mtime", 0.0),
                },
            )
        for place in result["places"]:
            _upsert_unit(
                worklist,
                kind="place",
                name=_unit_name_from_path(place["path"], home),
                path=place["path"],
                payload={
                    "category": place.get("category"),
                    "size_bytes": place.get("size_bytes", 0),
                    "last_mtime": place.get("last_mtime", 0.0),
                },
            )

        summary_msg = (
            f"found {len(result['projects'])} projects, "
            f"{len(result['places'])} places, "
            f"{len(result['errors'])} errors"
        )
        worklist.end_run(run_id, exit_code=0, summary=summary_msg)
    finally:
        worklist.close()

    table = Table(title="enumerate", show_header=True, header_style="bold")
    table.add_column("kind")
    table.add_column("count", justify="right")
    table.add_row("projects", str(len(result["projects"])))
    table.add_row("places", str(len(result["places"])))
    table.add_row("skipped", str(len(result["skipped"])))
    table.add_row("errors", str(len(result["errors"])))
    _console.print(table)
    _console.print(f"[green]wrote[/] {out_path}")


# ---------------------------------------------------------------------------
# Phase B — summary
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
# Phase C — rules
# ---------------------------------------------------------------------------


@app.command(help="Phase C: run deterministic rule plugins over enumerated units.")
def rules(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    system_dir = system_dir.resolve()
    db_path = system_dir / "worklist.sqlite"
    if not db_path.is_file():
        _console.print(
            f"[red]error:[/] no worklist at {db_path}. "
            "Run 'homing enumerate' first to populate it."
        )
        raise typer.Exit(code=2)

    worklist = Worklist(db_path)
    run_id = worklist.start_run("rules")
    try:
        report = orchestrator_module.run_rules(worklist)
        summary_msg = (
            f"{report.units_evaluated} units evaluated, "
            f"{report.units_needing_llm} need LLM, "
            f"{report.total_findings} findings persisted"
        )
        worklist.end_run(run_id, exit_code=0, summary=summary_msg)
    finally:
        worklist.close()

    table = Table(title="rules", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("units considered", str(report.total_units))
    table.add_row("evaluated (>= 0.7 confidence)", str(report.units_evaluated))
    table.add_row("needs-llm", str(report.units_needing_llm))
    table.add_row("findings persisted", str(report.total_findings))
    _console.print(table)

    if report.by_rule_counts:
        per_rule = Table(title="findings by rule", show_header=True, header_style="bold")
        per_rule.add_column("rule")
        per_rule.add_column("fired", justify="right")
        for name, count in report.by_rule_counts.items():
            per_rule.add_row(name, str(count))
        _console.print(per_rule)


# ---------------------------------------------------------------------------
# Phase D — classify
# ---------------------------------------------------------------------------

_DEFAULT_CLASSIFY_THRESHOLD = 0.5
_DEFAULT_CLASSIFY_BATCH_SIZE = 12


@app.command(
    help=(
        "Phase D: LLM classification for low-confidence units. With "
        "--via-orchestrator, emits batches under <system-dir>/batches/ for "
        "the orchestrating Claude Code session to fan out subagents on; no "
        "API key needed. Without --via-orchestrator, the standalone path is "
        "not yet implemented (set --via-orchestrator inside Claude Code)."
    )
)
def classify(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="System directory. Defaults to ~/system.",
    ),
    via_orchestrator: bool = typer.Option(
        False,
        "--via-orchestrator",
        help=(
            "Defer LLM calls to the orchestrating Claude Code session. "
            "Writes <system-dir>/batches/batch-NNN.json per unknown unit. "
            "After fan-out, run 'homing ingest-findings' to persist results. "
            "No ANTHROPIC_API_KEY needed."
        ),
    ),
    threshold: float = typer.Option(
        _DEFAULT_CLASSIFY_THRESHOLD,
        "--threshold",
        min=0.0,
        max=1.0,
        help=(
            "Confidence below which a unit is considered 'unknown' and "
            "needs LLM classification. Default 0.5."
        ),
    ),
    batch_size: int = typer.Option(
        _DEFAULT_CLASSIFY_BATCH_SIZE,
        "--batch-size",
        min=1,
        help="Number of units per orchestrator batch.",
    ),
) -> None:
    if not via_orchestrator:
        _console.print(
            "[red]error:[/] standalone classify (Anthropic API direct) is not "
            "yet implemented. Pass --via-orchestrator if you're inside a "
            "Claude Code session — that path is fully wired."
        )
        raise typer.Exit(code=1)

    system_dir = system_dir.expanduser().resolve()
    db_path = system_dir / "worklist.sqlite"
    if not db_path.is_file():
        _console.print(
            f"[red]error:[/] no worklist at {db_path}. "
            f"Run 'homing enumerate' (and ideally 'homing rules') first."
        )
        raise typer.Exit(code=2)

    # Identify units that need LLM classification: max rule-finding confidence
    # is below threshold, OR the unit has no findings at all.
    unknowns: list[dict[str, Any]] = []
    with Worklist(db_path) as wl:
        for unit in wl.all_units():
            findings = wl.findings_for(unit["name"])
            if findings:
                max_conf = max(float(f.get("confidence", 0.0)) for f in findings)
                if max_conf >= threshold:
                    continue
            # Compact rule context so the subagent has signal without
            # re-reading the worklist.
            rule_context = [
                {
                    "rule": f.get("rule"),
                    "confidence": f.get("confidence"),
                    "classifications": f.get("classifications"),
                }
                for f in findings
            ]
            unknowns.append(
                {
                    "name": unit["name"],
                    "kind": unit.get("kind"),
                    "path": unit.get("path"),
                    "metadata": unit.get("metadata") or {},
                    "rule_findings": rule_context,
                }
            )

    # Emit batches/ directory (always, even with 0 unknowns — explicit
    # completion signal so the orchestrator never hangs).
    batches_dir = system_dir / "batches"
    batches_dir.mkdir(parents=True, exist_ok=True)
    for old in batches_dir.glob("batch-*.json"):
        old.unlink()
    for old in batches_dir.glob("batch-*.results.json"):
        old.unlink()

    unknowns_path = system_dir / "unknowns.json"
    unknowns_path.write_text(json.dumps(unknowns, indent=2, default=str))

    n_batches = 0
    for i in range(0, len(unknowns), batch_size):
        batch = unknowns[i : i + batch_size]
        (batches_dir / f"batch-{i // batch_size:03d}.json").write_text(
            json.dumps(batch, indent=2, default=str)
        )
        n_batches += 1

    if not unknowns:
        _console.print(
            f"[green]--via-orchestrator:[/] 0 unknowns — every unit "
            f"classified above threshold {threshold} by deterministic "
            f"rules. No subagent fan-out needed.\n"
            f"[cyan]Next:[/] proceed to 'homing draft' / 'homing index'."
        )
    else:
        _console.print(
            f"[cyan]--via-orchestrator:[/] wrote {len(unknowns)} unknowns "
            f"as {n_batches} batch(es) of up to {batch_size} units to {batches_dir}\n"
            f"[cyan]Next:[/] orchestrator fans out subagents on each batch, "
            f"writes batch-NNN.results.json, then run "
            f"'homing ingest-findings --system-dir {system_dir}'."
        )


@app.command(name="ingest-findings")
def ingest_findings(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="System directory. Defaults to ~/system.",
    ),
    findings_file: Path = typer.Option(
        None,
        "--findings",
        help=(
            "Single findings JSON file. If omitted, reads "
            "<system-dir>/batches/batch-*.results.json."
        ),
    ),
    rule_name: str = typer.Option(
        "claude-code-subagent",
        "--rule-name",
        help="Rule name to record on each finding (for provenance).",
    ),
) -> None:
    """Ingest classification results from orchestrator-driven subagent runs.

    Each entry must have: name (unit slug), class_id, confidence, evidence
    (list of {path, reason} dicts or strings). Tolerates trailing data after
    the JSON array and missing fields. Empty evidence is auto-flagged as
    needs-human (citation rule).
    """
    system_dir = system_dir.expanduser().resolve()
    db_path = system_dir / "worklist.sqlite"
    if not db_path.is_file():
        _console.print(
            f"[red]error:[/] no worklist at {db_path}. "
            f"Run 'homing enumerate' first."
        )
        raise typer.Exit(code=2)

    if findings_file:
        files = [findings_file]
    else:
        files = sorted((system_dir / "batches").glob("*.results.json"))
        if not files:
            _console.print(
                f"[red]error:[/] no batch results in "
                f"{system_dir / 'batches'}. Did the orchestrator run?"
            )
            raise typer.Exit(code=2)

    written = 0
    failed = 0
    by_class: dict[str, int] = {}

    with Worklist(db_path) as wl:
        run_id = wl.start_run("ingest-findings")
        for f in files:
            text = f.read_text()
            try:
                results = json.loads(text)
            except json.JSONDecodeError:
                # Tolerate trailing prose after a valid JSON value.
                try:
                    results, _ = json.JSONDecoder().raw_decode(text)
                except Exception as exc:
                    _console.print(
                        f"[yellow]skip[/] {f.name}: unparseable ({exc})"
                    )
                    failed += 1
                    continue
            if not isinstance(results, list):
                results = [results]
            for r in results:
                try:
                    name = str(r["name"])
                    cid = str(r["class_id"])
                    conf = float(r.get("confidence", 0.5))
                    ev_in = r.get("evidence", [])
                    if isinstance(ev_in, str):
                        ev_items = [{"path": "(no path)", "reason": ev_in}]
                    elif isinstance(ev_in, list):
                        ev_items = []
                        for e in ev_in:
                            if isinstance(e, dict):
                                ev_items.append(
                                    {
                                        "path": e.get("path", "(no path)"),
                                        "reason": (
                                            e.get("reason")
                                            or e.get("note")
                                            or e.get("why")
                                            or str(e)
                                        ),
                                    }
                                )
                            else:
                                ev_items.append(
                                    {"path": "(no path)", "reason": str(e)}
                                )
                    else:
                        ev_items = [{"path": "(no path)", "reason": str(ev_in)}]
                    # Citation rule: empty evidence → flag for manual review
                    # rather than silently accepting an uncited classification.
                    if not ev_items:
                        ev_items = [
                            {
                                "path": "(subagent)",
                                "reason": "no evidence supplied — manual review required",
                            }
                        ]
                        cid = "needs-human"
                        conf = 0.0
                    if wl.unit(name) is None:
                        wl.event(
                            name=None,
                            type="orphan",
                            message=(
                                f"ingest-findings: result for {name!r} but "
                                f"no such unit in worklist"
                            ),
                        )
                        failed += 1
                        continue
                    wl.record_finding(
                        name,
                        rule_name,
                        confidence=max(0.0, min(1.0, conf)),
                        classifications={"class_id": cid},
                        evidence=ev_items,
                    )
                    try:
                        wl.update_status(name, "classified")
                    except Exception:
                        pass
                    by_class[cid] = by_class.get(cid, 0) + 1
                    written += 1
                except Exception as exc:
                    wl.event(
                        name=None,
                        type="ingest-error",
                        message=f"{f.name}: {exc}",
                    )
                    failed += 1
        run_exit_code = 1 if failed > 0 else 0
        wl.end_run(
            run_id,
            exit_code=run_exit_code,
            summary=f"ingested {written}, failed {failed}",
        )

    _console.print(
        f"[green]ingested:[/] {written}   [yellow]failed:[/] {failed}"
    )
    if by_class:
        for c, n in sorted(by_class.items(), key=lambda x: -x[1]):
            _console.print(f"  {c:30s} {n}")
    if failed > 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Phase E — draft (wired from draft_cli)
# ---------------------------------------------------------------------------

register_draft_command(app)


# ---------------------------------------------------------------------------
# Phase F — validate
# ---------------------------------------------------------------------------


_DEFAULT_VALIDATE_MODEL = "claude-sonnet-4-6"


def _agent_md_path_for(system_dir: Path, unit_name: str) -> Path:
    """Resolve the on-disk AGENT.md for a unit under ``<system-dir>``."""
    return system_dir.resolve() / "projects" / unit_name / "AGENT.md"


def _print_validation_result(result: validate_module.ValidationResult) -> None:
    """Render a ValidationResult as a rich-formatted block."""
    color = "green" if result.pass_threshold else "red"
    verdict = "PASS" if result.pass_threshold else "FAIL"
    _console.print(
        f"[bold {color}]{verdict}[/] [bold]{result.unit_name}[/] "
        f"confidence={result.confidence_score}/10  "
        f"({result.tokens_input}+{result.tokens_output} tok, "
        f"{result.elapsed_seconds:.1f}s)"
    )
    _console.print(f"  manifest: {result.agent_md_path}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("question", style="cyan", no_wrap=True)
    table.add_column("answer (truncated)")
    for key in validate_module.QUESTION_KEYS:
        if key == "wishlist":
            continue
        ans = result.answers.get(key, "")
        # Compact preview — full answer lives in the worklist.
        preview = ans.replace("\n", " ").strip()
        if len(preview) > 220:
            preview = preview[:217] + "..."
        table.add_row(key, preview or "(empty)")
    _console.print(table)

    if result.wishlist:
        _console.print("[bold]wishlist (gaps in AGENT.md):[/]")
        for item in result.wishlist:
            _console.print(f"  - {item}")


def _persist_validation(
    worklist: Worklist,
    result: validate_module.ValidationResult,
) -> None:
    """Record the validation as a worklist finding and bump status on pass."""
    unit = worklist.unit(result.unit_name)
    if unit is None:
        # No worklist entry for this name — skip persistence quietly. The
        # validator still printed its result; the user just isn't tied
        # into the cross-phase state machine.
        worklist.event(
            None,
            type="warn",
            message=(
                f"validate: unit {result.unit_name!r} not in worklist; "
                "finding not persisted"
            ),
        )
        return

    classifications = {
        "confidence_score": result.confidence_score,
        "pass": result.pass_threshold,
        "answers": result.answers,
        "wishlist": result.wishlist,
        "rationale": result.rationale,
        "model": result.model,
    }
    evidence = [(str(result.agent_md_path), "validation source")]
    worklist.record_finding(
        result.unit_name,
        rule="validate",
        confidence=result.confidence_score / 10.0,
        classifications=classifications,
        evidence=evidence,
    )
    if result.pass_threshold:
        try:
            worklist.update_status(result.unit_name, "validated")
        except (KeyError, ValueError) as e:
            worklist.event(
                result.unit_name,
                type="warn",
                message=f"validate: could not update status: {e}",
            )


@app.command(help="Phase F: fresh-agent validation of one or all manifests.")
def validate(
    name: Optional[str] = typer.Argument(
        None,
        help=(
            "Unit name to validate. Resolves to "
            "<system-dir>/projects/<name>/AGENT.md. "
            "Omit when --all is set."
        ),
    ),
    all_units: bool = typer.Option(
        False,
        "--all",
        help=(
            "Validate every <system-dir>/projects/*/AGENT.md found. "
            "Runs in serial to keep API spend predictable."
        ),
    ),
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="System directory. Defaults to ~/system.",
    ),
    model: str = typer.Option(
        _DEFAULT_VALIDATE_MODEL,
        "--model",
        help="Anthropic model id. Defaults to Sonnet (CLAUDE.md default).",
    ),
    via_orchestrator: bool = typer.Option(
        False,
        "--via-orchestrator",
        help=(
            "Defer LLM calls to the orchestrating Claude Code session. "
            "Writes a validation request bundle per AGENT.md to "
            "<system-dir>/validate-requests/. The orchestrator reads it, "
            "fires sub-agents, and writes results to "
            "<system-dir>/validate-results/. Run 'homing ingest-validations' "
            "afterward to persist findings to the worklist. No API key needed."
        ),
    ),
) -> None:
    if via_orchestrator:
        return _validate_via_orchestrator(name, all_units, system_dir, model)
    if all_units and name is not None:
        _console.print(
            "[red]error:[/] pass either a unit name OR --all, not both."
        )
        raise typer.Exit(code=2)
    if not all_units and name is None:
        _console.print(
            "[red]error:[/] supply a unit name or pass --all."
        )
        raise typer.Exit(code=2)

    system_dir = system_dir.resolve()
    db_path = system_dir / "worklist.sqlite"
    worklist: Optional[Worklist] = None
    if db_path.is_file():
        worklist = Worklist(db_path)
        run_id = worklist.start_run("validate")
    else:
        run_id = None

    pass_count = 0
    fail_count = 0
    error_count = 0
    score_sum = 0
    score_n = 0
    exit_code = 0

    try:
        if all_units:
            projects_dir = system_dir / "projects"
            if not projects_dir.is_dir():
                _console.print(
                    f"[red]error:[/] no projects directory at {projects_dir}"
                )
                raise typer.Exit(code=2)
            paths = sorted(projects_dir.glob("*/AGENT.md"))
            if not paths:
                _console.print(
                    f"[yellow]warn:[/] no AGENT.md files under {projects_dir}"
                )
                # Empty-set is technically a pass; exit 0.
                return
        else:
            assert name is not None
            single = _agent_md_path_for(system_dir, name)
            if not single.is_file():
                _console.print(
                    f"[red]error:[/] no AGENT.md at {single}. "
                    "Have you run `homing draft` for this unit?"
                )
                raise typer.Exit(code=2)
            paths = [single]

        for agent_md_path in paths:
            unit_name = agent_md_path.parent.name
            try:
                result = validate_module.validate_agent_md(
                    agent_md_path,
                    model=model,
                    unit_name=unit_name,
                )
            except (FileNotFoundError, ValueError) as e:
                error_count += 1
                exit_code = 1
                _console.print(
                    f"[red]error[/] validating {unit_name}: {e}"
                )
                if worklist is not None:
                    worklist.event(
                        None,
                        type="error",
                        message=f"validate {unit_name}: {e}",
                    )
                continue
            except Exception as e:  # noqa: BLE001 — surface SDK errors clearly
                error_count += 1
                exit_code = 1
                _console.print(
                    f"[red]error[/] LLM call failed for {unit_name}: {e}"
                )
                if worklist is not None:
                    worklist.event(
                        None,
                        type="error",
                        message=f"validate {unit_name}: {e}",
                    )
                continue

            _print_validation_result(result)
            score_sum += result.confidence_score
            score_n += 1
            if result.pass_threshold:
                pass_count += 1
            else:
                fail_count += 1
                # Single-name path returns nonzero on fail; --all path
                # surfaces failures via the summary table.
                if not all_units:
                    exit_code = 1

            if worklist is not None:
                _persist_validation(worklist, result)

        if all_units:
            avg = score_sum / score_n if score_n else 0.0
            summary_table = Table(
                title="validate summary", show_header=True, header_style="bold"
            )
            summary_table.add_column("metric")
            summary_table.add_column("value", justify="right")
            summary_table.add_row("manifests considered", str(len(paths)))
            summary_table.add_row("passed (>= 7/10)", str(pass_count))
            summary_table.add_row("failed", str(fail_count))
            summary_table.add_row("errored", str(error_count))
            summary_table.add_row("avg confidence", f"{avg:.2f}")
            _console.print(summary_table)
            if fail_count or error_count:
                exit_code = 1

        if worklist is not None and run_id is not None:
            summary_msg = (
                f"{pass_count} pass, {fail_count} fail, {error_count} error"
            )
            worklist.end_run(run_id, exit_code=exit_code, summary=summary_msg)
    finally:
        if worklist is not None:
            worklist.close()

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# Phase G — index
# ---------------------------------------------------------------------------


@app.command(help="Phase G: aggregate manifests into index.json.")
def index(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    system_dir = system_dir.resolve()
    system_dir.mkdir(parents=True, exist_ok=True)
    db_path = system_dir / "worklist.sqlite"

    worklist: Optional[Worklist] = None
    if db_path.is_file():
        worklist = Worklist(db_path)
        run_id = worklist.start_run("index")
    else:
        run_id = None

    try:
        payload = index_module.build_index(worklist, system_dir)
        out_path = system_dir / "index.json"
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if worklist is not None and run_id is not None:
            worklist.end_run(
                run_id,
                exit_code=0,
                summary=(
                    f"{payload['project_count']} projects, "
                    f"{payload['place_count']} places, "
                    f"{len(payload['warnings'])} warnings"
                ),
            )
    finally:
        if worklist is not None:
            worklist.close()

    table = Table(title="index", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("projects", str(payload["project_count"]))
    table.add_row("places", str(payload["place_count"]))
    table.add_row("warnings", str(len(payload["warnings"])))
    _console.print(table)

    if payload["warnings"]:
        _console.print("[yellow]warnings:[/]")
        for w in payload["warnings"]:
            _console.print(f"  - {w}")
    _console.print(f"[green]wrote[/] {out_path}")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


query_app = typer.Typer(help="Query the aggregated manifest index.")
app.add_typer(query_app, name="query")


def _load_index(system_dir: Path) -> dict[str, Any]:
    path = system_dir.resolve() / "index.json"
    if not path.is_file():
        _console.print(
            f"[red]error:[/] no index.json at {path}. Run 'homing index' first."
        )
        raise typer.Exit(code=2)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _all_units(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get("projects", [])) + list(payload.get("places", []))


@query_app.command("list", help="List every unit currently in the index.")
def query_list(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    payload = _load_index(system_dir)
    table = Table(title="units", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("state")
    table.add_column("purpose")
    table.add_column("last_meaningful_activity")

    units = _all_units(payload)
    units.sort(key=lambda u: (u.get("kind", ""), u.get("name", "")))
    for u in units:
        table.add_row(
            str(u.get("name", "")),
            str(u.get("kind", "")),
            str(u.get("state", u.get("status", ""))),
            str(u.get("purpose", "")),
            str(u.get("last_meaningful_activity", "")),
        )
    _console.print(table)
    _console.print(f"[dim]{len(units)} unit(s)[/]")


@query_app.command("show", help="Show a single unit as JSON.")
def query_show(
    name: str = typer.Argument(..., help="Unit name."),
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    payload = _load_index(system_dir)
    for u in _all_units(payload):
        if u.get("name") == name:
            _console.print_json(json.dumps(u, sort_keys=True))
            agent_path = u.get("agent_md_path") or u.get("place_md_path")
            if agent_path:
                _console.print(f"[green]manifest:[/] {agent_path}")
            return
    _console.print(f"[red]error:[/] no unit named {name!r} in index")
    raise typer.Exit(code=2)


@query_app.command("stale", help="List units with no meaningful activity in the last 90 days.")
def query_stale(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    payload = _load_index(system_dir)
    cutoff = datetime.now(timezone.utc).timestamp() - (_STALE_DAYS * _SECONDS_PER_DAY)

    table = Table(title=f"stale units (>{_STALE_DAYS} days)", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("last_meaningful_activity")

    stale = []
    for u in _all_units(payload):
        last = u.get("last_meaningful_activity")
        ts = _to_epoch(last)
        if ts is None or ts < cutoff:
            stale.append(u)
    stale.sort(key=lambda u: (str(u.get("last_meaningful_activity", "")), str(u.get("name", ""))))

    for u in stale:
        table.add_row(
            str(u.get("name", "")),
            str(u.get("kind", "")),
            str(u.get("last_meaningful_activity", "(unknown)")),
        )
    _console.print(table)
    _console.print(f"[dim]{len(stale)} stale unit(s)[/]")


@query_app.command("active", help="List units whose state is 'active'.")
def query_active(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR, "--system-dir", help="System directory. Defaults to ~/system."
    ),
) -> None:
    payload = _load_index(system_dir)
    table = Table(title="active units", show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("purpose")

    active = [u for u in _all_units(payload) if str(u.get("state", "")).lower() == "active"]
    active.sort(key=lambda u: str(u.get("name", "")))
    for u in active:
        table.add_row(
            str(u.get("name", "")),
            str(u.get("kind", "")),
            str(u.get("purpose", "")),
        )
    _console.print(table)
    _console.print(f"[dim]{len(active)} active unit(s)[/]")


# ---------------------------------------------------------------------------
# reconcile (stub)
# ---------------------------------------------------------------------------


@app.command(help="Reconcile a *.proposed.md manifest with an existing one (stub).")
def reconcile(name: str = typer.Argument(..., help="Unit name.")) -> None:
    del name
    _stub("reconcile")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config(override: Optional[Path]) -> dict[str, Any]:
    if override is not None:
        import yaml

        with override.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise typer.BadParameter(f"config at {override} is not a YAML mapping")
        return loaded
    return platform_module.load_config(platform_module.detect())


def _unit_name_from_path(path: str, home: Path) -> str:
    """Stable, unique-ish name for a unit derived from its absolute path.

    Uses the path relative to ``home`` with separators replaced by ``__`` so
    duplicate basenames in different parents don't collide. Falls back to
    the absolute path if the unit lives outside ``home``.
    """
    p = Path(path)
    try:
        rel = p.relative_to(home)
    except ValueError:
        # Not under home; flatten the absolute path.
        return str(p).replace("/", "__").lstrip("_") or p.name
    rel_str = rel.as_posix().replace("/", "__")
    return rel_str or p.name


def _upsert_unit(
    worklist: Worklist, *, kind: str, name: str, path: str, payload: dict[str, Any]
) -> None:
    """Insert a unit, ignoring duplicates so re-runs are idempotent."""
    if worklist.unit(name) is not None:
        # Already present; we leave its status alone (later phases own it)
        # but refresh the payload so re-enumeration captures fresh signals.
        # The worklist API doesn't expose an update_payload helper; instead
        # we record an event so the change is auditable.
        worklist.event(name, type="info", message="re-enumerated; payload unchanged in DB")
        return
    worklist.add_unit(kind=kind, name=name, path=path, payload=payload)


def _to_epoch(value: Any) -> Optional[float]:
    """Best-effort conversion of an ISO timestamp / numeric value to epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None



# ---------------------------------------------------------------------------
# Phase F — validate via orchestrator (no API key needed)
# ---------------------------------------------------------------------------


def _validate_via_orchestrator(
    name: Optional[str],
    all_units: bool,
    system_dir: Path,
    model: str = _DEFAULT_VALIDATE_MODEL,
) -> None:
    """Emit validation request files for the orchestrator to handle."""
    import json as _json

    if all_units and name is not None:
        _console.print("[red]error:[/] pass either a unit name OR --all, not both.")
        raise typer.Exit(code=2)
    if not all_units and name is None:
        _console.print("[red]error:[/] supply a unit name or pass --all.")
        raise typer.Exit(code=2)

    system_dir = system_dir.resolve()

    if all_units:
        projects_dir = system_dir / "projects"
        if not projects_dir.is_dir():
            _console.print(f"[red]error:[/] no projects directory at {projects_dir}")
            raise typer.Exit(code=2)
        agent_md_paths = sorted(projects_dir.glob("*/AGENT.md"))
    else:
        agent_md_paths = [system_dir / "projects" / name / "AGENT.md"]

    requests_dir = system_dir / "validate-requests"
    results_dir = system_dir / "validate-results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for agent_md in agent_md_paths:
        if not agent_md.is_file():
            _console.print(f"[yellow]skip:[/] {agent_md} (not a file)")
            continue
        unit_name = agent_md.parent.name
        agent_md_text = agent_md.read_text(encoding="utf-8")
        user_msg = validate_module._build_user_message(agent_md, agent_md_text)
        request = {
            "unit_name": unit_name,
            "agent_md_path": str(agent_md),
            "user_message": user_msg,
            "questions": dict(validate_module.QUESTIONS),
            "result_path": str(results_dir / f"{unit_name}.json"),
            "model": model,
            "instructions": (
                "You are the orchestrator. Spawn a fresh sub-agent that has NOT seen this "
                "project before. Pass it the user_message verbatim. The sub-agent must answer "
                "every question and emit a JSON object with: confidence_score (1-10), "
                "answers (dict keyed by question), wishlist (list of <=3 strings). "
                "Write the JSON to result_path. Then run 'homing ingest-validations' to persist. "
                "The 'model' field is the suggested model; the orchestrator may pick a different one."
            ),
        }
        (requests_dir / f"{unit_name}.json").write_text(_json.dumps(request, indent=2))
        written += 1

    _console.print(
        f"[cyan]validate --via-orchestrator:[/cyan] wrote {written} request(s) to {requests_dir}\n"
        f"[cyan]Results expected at:[/cyan] {results_dir}\n"
        f"[cyan]Next:[/cyan] orchestrator fans out sub-agents, then 'homing ingest-validations'."
    )


@app.command(name="ingest-validations")
def ingest_validations(
    system_dir: Path = typer.Option(
        _DEFAULT_SYSTEM_DIR,
        "--system-dir",
        help="System directory. Defaults to ~/system.",
    ),
) -> None:
    """Ingest sub-agent-produced validation results into the worklist.

    Reads <system-dir>/validate-results/<unit>.json files. Each must contain
    confidence_score, answers, wishlist. Tolerates missing/extra fields.
    """
    import json as _json

    system_dir = system_dir.resolve()
    results_dir = system_dir / "validate-results"
    if not results_dir.is_dir():
        _console.print(f"[red]error:[/] no {results_dir}. Run 'homing validate --via-orchestrator' first.")
        raise typer.Exit(code=2)

    db_path = system_dir / "worklist.sqlite"
    # Hard-fail when the worklist is absent: silently exiting 0 here looks like
    # success but persists nothing, which broke the audit trail. The user must
    # run `homing enumerate` (or whichever command writes the worklist) first.
    if not db_path.is_file():
        _console.print(
            f"[red]error:[/] no worklist at {db_path}. "
            f"Run 'homing enumerate' first so we have units to attach findings to."
        )
        raise typer.Exit(code=2)

    worklist = Worklist(db_path)
    run_id = worklist.start_run("ingest-validations")

    written = 0
    failed = 0
    pass_n = 0
    fail_n = 0

    try:
        for f in sorted(results_dir.glob("*.json")):
            try:
                data = _json.loads(f.read_text())
                unit_name = data.get("unit_name") or f.stem
                score = int(data.get("confidence_score", 0))
                score = max(0, min(10, score))
                passed = score >= validate_module.PASS_THRESHOLD
                answers = data.get("answers", {})
                wishlist = data.get("wishlist", [])
                # Persist the full answers + wishlist alongside the score —
                # the score alone loses the qualitative reasoning we asked the
                # sub-agent to produce. Mirror the shape `_persist_validation`
                # writes for the direct (non-orchestrator) path so consumers
                # querying findings see one schema regardless of code path.
                classifications = {
                    "score": score,
                    "pass": passed,
                    "answers": answers,
                    "wishlist": wishlist,
                    "rationale": data.get("rationale", ""),
                    "model": data.get("model", ""),
                }
                # The unit may not exist in the worklist yet (e.g. AGENT.md
                # was hand-written for a project not yet enumerated). Probe
                # first so we can attach an orphan event with name=None
                # instead of triggering a KeyError cascade where the rescue
                # path itself raises.
                unit_exists = worklist.unit(unit_name) is not None
                if unit_exists:
                    try:
                        worklist.record_finding(
                            unit_name,
                            rule="validate",
                            confidence=score / 10.0,
                            classifications=classifications,
                            evidence=[
                                {"path": str(f), "reason": "orchestrator validation result"}
                            ],
                        )
                        if passed:
                            try:
                                worklist.update_status(unit_name, "validated")
                            except (KeyError, ValueError):
                                # Race: unit deleted between probe and update,
                                # or "validated" not in VALID_STATUSES on this
                                # schema version. Non-fatal — finding is still
                                # recorded.
                                pass
                    except Exception as e:
                        # Use name=None for the warn event so we don't recurse
                        # into _unit_id_or_raise on a unit we've just shown
                        # has issues.
                        worklist.event(
                            name=None,
                            type="warn",
                            message=f"ingest-validations: {unit_name}: {e}",
                        )
                else:
                    worklist.event(
                        name=None,
                        type="orphan",
                        message=(
                            f"ingest-validations: result for {unit_name!r} but "
                            f"no such unit in worklist (run 'homing enumerate'?)"
                        ),
                    )
                if passed:
                    pass_n += 1
                else:
                    fail_n += 1
                written += 1
            except Exception as exc:
                _console.print(f"[yellow]skip[/] {f.name}: {exc}")
                failed += 1
        # Exit code reflects parse failures so callers (CI, the orchestrator)
        # can distinguish "all clean" from "ingested some, but corrupt files
        # need attention." Worklist persistence still happens for everything
        # parseable.
        run_exit_code = 1 if failed > 0 else 0
        worklist.end_run(
            run_id,
            exit_code=run_exit_code,
            summary=f"ingested {written}, failed {failed}",
        )
    finally:
        worklist.close()

    _console.print(
        f"[green]ingested:[/] {written}   "
        f"[bold]pass:[/] {pass_n}   [bold]fail:[/] {fail_n}   "
        f"[yellow]parse-errors:[/] {failed}"
    )
    if failed > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()

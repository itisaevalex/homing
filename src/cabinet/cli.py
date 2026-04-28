"""cabinet CLI — `cabinet scan ...` and stubs for later phases.

Phase A implements `scan`. Other commands (classify, triage, reconcile, plan,
apply, undo, query) are stubbed and will be filled in by other agents.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .enumerate import enumerate_paths
from .homogeneity import score_folder
from .sampler import sample_files
from .worklist import Worklist

app = typer.Typer(
    name="cabinet",
    help="Triage messy personal-document folders without losing anything.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()

DEFAULT_OUTPUT_DIR = Path.home() / "cabinet"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cabinet {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """cabinet — triage personal-document folders, reversibly."""
    return None


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@app.command()
def scan(
    paths: list[Path] = typer.Argument(
        ..., exists=False, help="One or more directories to walk."
    ),
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        "--output",
        help="Where to write scan_result.json and the worklist database.",
    ),
    max_depth: int = typer.Option(
        8, "--max-depth", help="Maximum directory depth (root = 0)."
    ),
    sample_k: int = typer.Option(
        5, "--sample-k", help="How many representative files to sample per folder."
    ),
) -> None:
    """Walk PATHS, score each folder, sample representatives, write results.

    Read-only. Never modifies source files. Writes only to OUTPUT_DIR.
    """
    expanded_paths = [p.expanduser() for p in paths]
    missing = [p for p in expanded_paths if not p.exists()]
    if missing:
        for p in missing:
            console.print(f"[red]error[/red]: path does not exist: {p}")
        raise typer.Exit(code=2)

    output_dir = output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    worklist_path = output_dir / "worklist.db"
    scan_json_path = output_dir / "scan_result.json"

    console.print(f"[bold]cabinet scan[/bold] {len(expanded_paths)} path(s) → {output_dir}")
    for p in expanded_paths:
        console.print(f"  • {p}")

    with Worklist(worklist_path) as worklist:
        run_id = worklist.start_run("scan")

        result = enumerate_paths(expanded_paths, max_depth=max_depth)

        # Score each folder, sample, persist into worklist + scan_result.json.
        verdict_counter: Counter[str] = Counter()
        per_folder_payload: list[dict] = []

        for folder in result.folders:
            score = score_folder(folder)
            samples = sample_files(folder, k=sample_k, strategy="stratified")

            verdict_counter[score.verdict] += 1

            metadata = {
                "depth": folder.depth,
                "file_count": folder.file_count,
                "total_size": folder.total_size,
                "file_extensions": folder.file_extensions,
                "date_range": list(folder.date_range) if folder.date_range else None,
                "homogeneity": score.to_dict(),
                "samples": [s.to_dict() for s in samples],
            }
            unit_id = worklist.add_unit(
                "folder", folder.path, status="scanned", metadata=metadata
            )

            # Also register file-level units when the folder isn't classifiable
            # as a whole — those are the files the classifier will hit per-file.
            if score.verdict == "per-file":
                for fmeta in folder.files:
                    worklist.add_unit(
                        "file",
                        fmeta.path,
                        status="discovered",
                        metadata=fmeta.to_dict(),
                    )

            per_folder_payload.append(
                {
                    "folder": folder.to_dict(),
                    "homogeneity": score.to_dict(),
                    "samples": [s.to_dict() for s in samples],
                    "unit_id": unit_id,
                }
            )

        scan_payload = {
            "schema_version": 1,
            "cabinet_version": __version__,
            "roots": list(result.roots),
            "totals": {
                "folders": result.total_folders,
                "files": result.total_files,
                "size_bytes": result.total_size,
            },
            "verdicts": dict(verdict_counter),
            "skipped": list(result.skipped),
            "folders": per_folder_payload,
        }

        # Sorted JSON keys for byte-stable output.
        scan_json_path.write_text(json.dumps(scan_payload, sort_keys=True, indent=2))

        worklist.end_run(
            run_id,
            summary={
                "folders": result.total_folders,
                "files": result.total_files,
                "verdicts": dict(verdict_counter),
                "scan_result_path": str(scan_json_path),
            },
        )

    _print_summary(result, verdict_counter, scan_json_path, worklist_path)


def _print_summary(result, verdicts, scan_json_path: Path, worklist_path: Path) -> None:
    table = Table(title="Scan summary", show_header=True, header_style="bold")
    table.add_column("metric", style="cyan")
    table.add_column("value", justify="right")
    table.add_row("roots", str(len(result.roots)))
    table.add_row("folders", str(result.total_folders))
    table.add_row("files", str(result.total_files))
    table.add_row("total size", _humanize_bytes(result.total_size))
    table.add_row("skipped", str(len(result.skipped)))
    console.print(table)

    verdicts_table = Table(title="Homogeneity verdicts", show_header=True, header_style="bold")
    verdicts_table.add_column("verdict", style="cyan")
    verdicts_table.add_column("count", justify="right")
    for v in ("folder-classifiable", "subdivide", "per-file"):
        verdicts_table.add_row(v, str(verdicts.get(v, 0)))
    console.print(verdicts_table)

    console.print(f"\nscan_result.json → [green]{scan_json_path}[/green]")
    console.print(f"worklist.db      → [green]{worklist_path}[/green]")


def _humanize_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# stubs for later phases
# ---------------------------------------------------------------------------


def _stub(name: str) -> None:
    console.print(f"[yellow]cabinet {name}[/yellow]: not yet implemented (Phase pending).")
    raise typer.Exit(code=1)


@app.command()
def classify(
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        "--system-dir",
        help="Where the worklist lives. Defaults to ~/cabinet/.",
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help="Skip the LLM cascade tier. Use deterministic rules only.",
    ),
    no_vision: bool = typer.Option(
        False,
        "--no-vision",
        help="Disable multimodal vision calls (text-LLM only).",
    ),
    via_orchestrator: bool = typer.Option(
        False,
        "--via-orchestrator",
        help=(
            "Defer LLM-tier classification to the orchestrating Claude Code session. "
            "Runs deterministic rules, then writes unclassified units to "
            "<output_dir>/unknowns.json. The orchestrator (you) reads it, classifies via "
            "subagents, and ingests results with 'cabinet ingest-findings'. No API key needed."
        ),
    ),
    batch_size: int = typer.Option(
        12,
        "--batch-size",
        help="Number of units per orchestrator batch (only with --via-orchestrator).",
    ),
) -> None:
    """Phase B — run deterministic rules then escalate to LLM where needed.

    Three modes:
      - default: run rules then anthropic-SDK LLM (needs ANTHROPIC_API_KEY)
      - --no-llm: rules only, no escalation
      - --via-orchestrator: rules then defer to the orchestrating Claude Code
        session (writes unknowns.json, no API key needed)
    """
    from cabinet.worklist import Worklist
    from cabinet.classifier import classify_unit
    from cabinet.rules.base import UnitContext
    import os
    import json as _json

    output_dir = output_dir.expanduser().resolve()
    db_path = output_dir / "worklist.db"
    if not db_path.is_file():
        console.print(f"[red]error:[/red] no worklist at {db_path}. Run 'cabinet scan' first.")
        raise typer.Exit(code=2)

    if via_orchestrator and no_llm:
        console.print("[red]error:[/red] --via-orchestrator and --no-llm are incompatible. Pick one.")
        raise typer.Exit(code=2)

    client = None
    if not no_llm and not via_orchestrator:
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                from anthropic import Anthropic
                client = Anthropic()
            except Exception as exc:
                console.print(f"[yellow]anthropic client init failed: {exc}; rules-only.[/yellow]")
        else:
            console.print(
                "[yellow]ANTHROPIC_API_KEY not set — running rules-only. "
                "Tip: pass --via-orchestrator if you're inside Claude Code "
                "and want subagents to handle unknowns instead.[/yellow]"
            )

    wl = Worklist(db_path)
    run_id = wl.start_run("classify")
    try:
        units = wl.units_by_status("scanned") + wl.units_by_status("discovered")
        evaluated = 0
        unknown = 0
        for unit in units:
            try:
                meta = unit.metadata or {}
                samples = meta.get("samples") or []
                # Phase A stores samples as list of {path, content_hash, ...} dicts
                sample_paths = [Path(s["path"]) for s in samples if isinstance(s, dict) and "path" in s]
                date_range = meta.get("date_range")
                if isinstance(date_range, list) and len(date_range) == 2:
                    date_range = (float(date_range[0]), float(date_range[1]))
                elif not (isinstance(date_range, tuple) and len(date_range) == 2):
                    date_range = None
                ctx = UnitContext(
                    path=Path(unit.path),
                    kind=unit.kind,
                    extensions=meta.get("file_extensions") or meta.get("extensions") or {},
                    file_count=int(meta.get("file_count") or (1 if unit.kind == "file" else 0)),
                    total_size=int(meta.get("total_size") or meta.get("size") or 0),
                    date_range=date_range,
                    sample_paths=sample_paths,
                    sample_contents={},
                    sample_exif={},
                    siblings=meta.get("siblings", []),
                    parent_name=Path(unit.path).parent.name,
                )
                result = classify_unit(ctx, anthropic_client=client, allow_vision=not no_vision)
                if result is not None and result.class_id:
                    wl.record_finding(
                        unit.id,
                        result.rule_name,
                        confidence=float(result.confidence),
                        classifications=[result.class_id],
                        evidence={"items": [{"path": str(p), "reason": r} for (p, r) in result.evidence]},
                    )
                    try:
                        wl.update_status(unit.id, "classified")
                    except Exception:
                        pass  # status enum may not include "classified" — finding is recorded regardless
                    evaluated += 1
                else:
                    unknown += 1
            except Exception as exc:
                wl.event("classify-error", unit_id=unit.id, payload={"message": str(exc)})
                unknown += 1
        wl.end_run(run_id, summary={"classified": evaluated, "unknown": unknown})
    finally:
        wl.close()

    console.print(f"[green]classified:[/green] {evaluated}   [yellow]unknown:[/yellow] {unknown}")

    # --via-orchestrator: always emit batches/ — even when 0 unknowns, an
    # explicit empty batch tells the orchestrating Claude Code session "we're
    # done here" instead of leaving it polling forever.
    if via_orchestrator:
        wl = Worklist(db_path)
        try:
            still_unknown = []
            for u in wl.all_units():
                if not wl.findings_for(u.id):
                    still_unknown.append({
                        "unit_id": u.id,
                        "path": u.path,
                        "kind": u.kind,
                        "metadata": u.metadata,
                    })
        finally:
            wl.close()

        unknowns_path = output_dir / "unknowns.json"
        unknowns_path.write_text(_json.dumps(still_unknown, indent=2, default=str))

        batches_dir = output_dir / "batches"
        batches_dir.mkdir(exist_ok=True)
        # Clean previous batch files
        for old in batches_dir.glob("batch-*.json"):
            old.unlink()
        for old in batches_dir.glob("batch-*.results.json"):
            old.unlink()

        n_batches = 0
        for i in range(0, len(still_unknown), batch_size):
            batch = still_unknown[i:i + batch_size]
            (batches_dir / f"batch-{i // batch_size:03d}.json").write_text(
                _json.dumps(batch, indent=2, default=str)
            )
            n_batches += 1

        if not still_unknown:
            console.print(
                f"[green]--via-orchestrator:[/green] 0 unknowns — every unit "
                f"classified by deterministic rules. No subagent fan-out needed.\n"
                f"[cyan]Next:[/cyan] proceed to 'cabinet triage'."
            )
        else:
            console.print(
                f"[cyan]--via-orchestrator:[/cyan] wrote {len(still_unknown)} unknowns "
                f"as {n_batches} batches of up to {batch_size} units to {batches_dir}\n"
                f"[cyan]Next:[/cyan] the orchestrator should fan out subagents on each batch, "
                f"then run 'cabinet ingest-findings --output-dir {output_dir}'."
            )


@app.command(name="ingest-findings")
def ingest_findings(
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        "--system-dir",
        help="Cabinet system dir.",
    ),
    findings_file: Path = typer.Option(
        None,
        "--findings",
        help="Single findings JSON file. If omitted, reads <output_dir>/batches/batch-*.results.json.",
    ),
    rule_name: str = typer.Option(
        "claude-code-subagent",
        "--rule-name",
        help="Rule name to record on each finding (for provenance).",
    ),
) -> None:
    """Ingest classification results from orchestrator-driven subagent runs.

    Each entry must have: unit_id, class_id, confidence, evidence (list of
    {path, reason} dicts or strings). Tolerates trailing data after the JSON
    array (some agents add closing prose) and missing fields.
    """
    from cabinet.worklist import Worklist
    import json as _json

    output_dir = output_dir.expanduser().resolve()
    db_path = output_dir / "worklist.db"
    if not db_path.is_file():
        console.print(f"[red]error:[/red] no worklist at {db_path}. Run 'cabinet scan' first.")
        raise typer.Exit(code=2)

    if findings_file:
        files = [findings_file]
    else:
        files = sorted((output_dir / "batches").glob("*.results.json"))
        if not files:
            console.print(f"[red]error:[/red] no batch results in {output_dir / 'batches'}.")
            raise typer.Exit(code=2)

    wl = Worklist(db_path)
    run_id = wl.start_run("ingest-findings")
    written = 0
    failed = 0
    by_class: dict[str, int] = {}
    try:
        for f in files:
            text = f.read_text()
            try:
                results = _json.loads(text)
            except _json.JSONDecodeError:
                # tolerate trailing prose/garbage after a valid JSON value
                try:
                    results, _ = _json.JSONDecoder().raw_decode(text)
                except Exception as exc:
                    console.print(f"[yellow]skip[/yellow] {f.name}: unparseable ({exc})")
                    continue
            if not isinstance(results, list):
                results = [results]
            for r in results:
                try:
                    uid = int(r["unit_id"])
                    cid = str(r["class_id"])
                    conf = float(r.get("confidence", 0.5))
                    ev_in = r.get("evidence", [])
                    # Normalize evidence: accept list-of-dicts, list-of-strings, or single string
                    if isinstance(ev_in, str):
                        ev_items = [{"path": "(no path)", "reason": ev_in}]
                    elif isinstance(ev_in, list):
                        ev_items = []
                        for e in ev_in:
                            if isinstance(e, dict):
                                ev_items.append({
                                    "path": e.get("path", "(no path)"),
                                    "reason": e.get("reason") or e.get("note") or e.get("why") or str(e),
                                })
                            else:
                                ev_items.append({"path": "(no path)", "reason": str(e)})
                    else:
                        ev_items = [{"path": "(no path)", "reason": str(ev_in)}]
                    # Citation rule: every classification must cite at least
                    # one path/reason. An empty evidence list is treated as a
                    # subagent failure — flag for human review rather than
                    # silently accepting an uncited classification.
                    if not ev_items:
                        ev_items = [{
                            "path": "(subagent)",
                            "reason": "no evidence supplied — manual review required",
                        }]
                        cid = "needs-human"
                        conf = min(conf, 0.0)
                    wl.record_finding(
                        uid,
                        rule_name,
                        confidence=max(0.0, min(1.0, conf)),
                        classifications=[cid],
                        evidence={"items": ev_items},
                    )
                    try:
                        wl.update_status(uid, "classified")
                    except Exception:
                        pass
                    by_class[cid] = by_class.get(cid, 0) + 1
                    written += 1
                except Exception as exc:
                    wl.event("ingest-error", payload={"file": f.name, "error": str(exc)})
                    failed += 1
        wl.end_run(run_id, summary={"written": written, "failed": failed})
    finally:
        wl.close()

    console.print(f"[green]ingested:[/green] {written}   [yellow]failed:[/yellow] {failed}")
    if by_class:
        for c, n in sorted(by_class.items(), key=lambda x: -x[1]):
            console.print(f"  {c:30s} {n}")


# Phases C–F wired from their module register_* functions.
from cabinet.triage import register_triage_command
from cabinet.reconcile import register_reconcile_command
from cabinet.planner import register_plan_command
from cabinet.undo import register_apply_command, register_undo_command

register_triage_command(app)
register_reconcile_command(app)
register_plan_command(app)
register_apply_command(app)
register_undo_command(app)


@app.command()
def query() -> None:
    """Query classifications, duplicates, content types. Stub for now."""
    _stub("query")


if __name__ == "__main__":
    app()

"""Phase C — generate the human-readable triage manifest.

The triage manifest is the single artifact a user reviews. It must be:
- Readable in 30 minutes for a typical run.
- Decidable in batches — biggest decisions first.
- Honest about evidence — every classification cites the signal that drove it.
- Safe by default — sensitive units (passport, tax docs, contracts) are flagged.

This module is pure markdown rendering — it never touches source files.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# Action labels exposed to the user as checkbox lines.
# These string constants are the contract between triage.md output and
# reconcile.py parsing — keep them stable across versions.
ACTION_KEEP = "keep"
ACTION_ARCHIVE = "archive"
ACTION_DEDUPE = "dedupe"
ACTION_REVIEW_LATER = "review later (skip)"
ACTION_TRASH = "trash (move to ~/cabinet-review-pile/)"

ALL_ACTIONS: tuple[str, ...] = (
    ACTION_KEEP,
    ACTION_ARCHIVE,
    ACTION_DEDUPE,
    ACTION_REVIEW_LATER,
    ACTION_TRASH,
)

# Sensitive classifications get a warning marker. Keep this aligned with
# config/content_classes.yaml `sensitive: true` entries.
SENSITIVE_CLASSES: frozenset[str] = frozenset(
    {
        "scan-of-id",
        "tax-document",
        "contract",
        "certificate",
    }
)


@dataclass(frozen=True, slots=True)
class TriageUnit:
    """A unit (folder or file) ready for triage rendering.

    Designed to be built by Phase A/B (enumerate, classify) and consumed here
    read-only. Immutable so triage rendering can't accidentally mutate state.

    Field semantics:
        unit_id: stable identifier (typically the unit path).
        path: absolute filesystem path to the unit.
        kind: "folder" | "file".
        classification: id from config/content_classes.yaml; "unknown" allowed.
        confidence: 0..1 confidence the classifier assigned.
        evidence_source: short tag (e.g. "by_exif", "by_extension", "llm_text").
        evidence_notes: bullet strings rendered verbatim under "Evidence:".
        file_count, total_size: aggregate stats for folders; for files: 1, size.
        date_range: (epoch_min, epoch_max) or None.
        suggested_action: one of ALL_ACTIONS — what cabinet would do if forced.
        suggested_archive_dest: optional pre-filled dest for archive line.
        duplicate_group: optional id grouping duplicate units; emit a paired
            section when more than one unit shares the same group.
    """

    unit_id: str
    path: str
    kind: str
    classification: str
    confidence: float
    evidence_source: str
    evidence_notes: tuple[str, ...]
    file_count: int
    total_size: int
    date_range: tuple[float, float] | None = None
    suggested_action: str = ACTION_KEEP
    suggested_archive_dest: str | None = None
    duplicate_group: str | None = None

    @property
    def is_sensitive(self) -> bool:
        return self.classification in SENSITIVE_CLASSES


class WorklistReader(Protocol):
    """Minimal interface triage needs from the worklist.

    Any object exposing ``get_triage_units()`` works — this keeps triage.py
    decoupled from the SQLite-backed worklist owned by Phase A.
    """

    def get_triage_units(self) -> Sequence[TriageUnit]:
        ...


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    """Human-readable size — KB, MB, GB. Matches the size formatting users see in file managers."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    value = float(num_bytes)
    unit_idx = -1
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024
        unit_idx += 1
    if unit_idx < 0:
        return f"{num_bytes} B"
    return f"{value:.1f} {units[unit_idx]}"


def _format_date_range(date_range: tuple[float, float] | None) -> str:
    if date_range is None:
        return "no dates"
    lo, hi = date_range
    lo_str = _dt.datetime.fromtimestamp(lo).strftime("%Y-%m-%d")
    hi_str = _dt.datetime.fromtimestamp(hi).strftime("%Y-%m-%d")
    if lo_str == hi_str:
        return f"date {lo_str}"
    return f"dates {lo_str} → {hi_str}"


def _render_unit_section(unit: TriageUnit, *, sensitive: bool) -> str:
    """Render one unit as a markdown section with checkboxes.

    Each unit is its own section so the user can decide one at a time. The
    checkbox list uses the exact action strings from ALL_ACTIONS; reconcile.py
    re-parses them.
    """
    header = f"## {unit.path}"
    if sensitive:
        header = f"## ⚠ {unit.path}"

    lines: list[str] = [header, ""]

    if sensitive:
        lines.append(
            "> **Look at this carefully — sensitive content (PII / legal / financial).**"
        )
        lines.append("")

    lines.append(
        f"- Classification: `{unit.classification}` "
        f"(confidence {unit.confidence:.2f}, by {unit.evidence_source})"
    )
    size_str = _format_size(unit.total_size)
    if unit.kind == "folder":
        lines.append(f"- {unit.file_count:,} files, {size_str}, {_format_date_range(unit.date_range)}")
    else:
        lines.append(f"- 1 file, {size_str}, {_format_date_range(unit.date_range)}")

    if unit.evidence_notes:
        ev = "; ".join(unit.evidence_notes)
        lines.append(f"- Evidence: {ev}")

    lines.append("- Decision (mark ONE):")
    for action in ALL_ACTIONS:
        suggested = action == unit.suggested_action
        suffix = ""
        if action == ACTION_ARCHIVE and unit.suggested_archive_dest:
            suffix = f" → {unit.suggested_archive_dest}"
        marker = "x" if False else " "  # never pre-fill; user marks ONE.
        # We do NOT pre-mark; user must explicitly check. Suggested is a hint
        # rendered as a comment so the dry-run intent is unambiguous.
        suggestion = "  *(suggested)*" if suggested else ""
        lines.append(f"  - [{marker}] {action}{suffix}{suggestion}")

    lines.append("")
    return "\n".join(lines)


def _render_dedupe_pair_section(group_id: str, members: Sequence[TriageUnit]) -> str:
    """Render a dedupe-paired section asking which copy to keep.

    Triggered when 2+ units share a duplicate_group. The user marks ONE copy
    keep; the rest get the dedupe action.
    """
    lines: list[str] = [
        f"## DEDUPE GROUP `{group_id}` — {len(members)} copies",
        "",
        "> Pick ONE copy to keep. Others will move to the review pile under "
        "`dedupe/` (reversible).",
        "",
    ]
    # Include classification info from any member; copies are by definition
    # the same content, so first member is fine.
    rep = members[0]
    lines.append(f"- Classification: `{rep.classification}` (by {rep.evidence_source})")
    lines.append(f"- Per-copy size: {_format_size(rep.total_size)}")
    lines.append("")
    lines.append("Copies (mark exactly one as keep):")
    for m in members:
        lines.append(f"- [ ] keep: `{m.path}`")
    lines.append("")
    return "\n".join(lines)


def _hypothetical_bytes(units: Iterable[TriageUnit], *, action: str) -> int:
    """How many bytes would be moved if every unit took ``action``?"""
    return sum(u.total_size for u in units)


def _group_key(unit: TriageUnit) -> tuple[str, int]:
    """Sort key: by classification (stable string), then by negative size."""
    # Negative for descending size, stable on classification string.
    return (unit.classification, -unit.total_size)


def _render_header(units: Sequence[TriageUnit], *, generated_at: _dt.datetime) -> str:
    total = len(units)
    total_bytes = _hypothetical_bytes(units, action="archive")  # same regardless

    lines = [
        "# Cabinet triage manifest",
        "",
        f"_Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        f"- {total:,} units to review",
        f"- Total bytes affected if every unit was **archived** or **trashed**: {_format_size(total_bytes)}",
        "",
        "## How to use",
        "",
        "1. For each unit below, mark ONE checkbox.",
        "2. Default is `keep` — when in doubt, keep.",
        "3. `trash` does NOT delete — it moves to a review pile you can empty later.",
        "4. After marking, save and run `cabinet reconcile`.",
        "",
        "Sections marked ⚠ are sensitive (passport, tax, contracts) — review carefully.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def render_triage_markdown(
    units: Sequence[TriageUnit],
    *,
    generated_at: _dt.datetime | None = None,
) -> str:
    """Render the full triage manifest as a single markdown string.

    File order: by classification (groups), then by size descending within group.
    Dedupe groups (>1 member) get rendered as a single paired section per group,
    BEFORE their classification block, so the user resolves dedupes first.
    """
    if generated_at is None:
        generated_at = _dt.datetime.now()

    # Bucket dedupe groups separately. A unit with no dup-group → normal flow.
    dedupe_groups: dict[str, list[TriageUnit]] = {}
    singletons: list[TriageUnit] = []
    for u in units:
        if u.duplicate_group:
            dedupe_groups.setdefault(u.duplicate_group, []).append(u)
        else:
            singletons.append(u)

    # Some "duplicate_group" tags may turn out to have a single member —
    # treat those as singletons.
    promoted: list[TriageUnit] = []
    real_groups: dict[str, list[TriageUnit]] = {}
    for gid, members in dedupe_groups.items():
        if len(members) <= 1:
            promoted.extend(members)
        else:
            real_groups[gid] = members
    singletons.extend(promoted)

    # Sort singletons by group key (classification, -size).
    singletons.sort(key=_group_key)

    # Sort dedupe groups by group id for determinism.
    parts: list[str] = [_render_header(units, generated_at=generated_at)]

    if real_groups:
        parts.append("# Duplicate groups (resolve first)\n")
        for gid in sorted(real_groups):
            members = sorted(real_groups[gid], key=lambda m: m.path)
            parts.append(_render_dedupe_pair_section(gid, members))

    if singletons:
        parts.append("# Per-unit decisions\n")
        # Insert classification subheaders.
        current_class: str | None = None
        for u in singletons:
            if u.classification != current_class:
                parts.append(f"### Class: `{u.classification}`\n")
                current_class = u.classification
            parts.append(_render_unit_section(u, sensitive=u.is_sensitive))

    # Trailing newline keeps git/editor happy.
    return "\n".join(parts).rstrip() + "\n"


def write_triage_report(
    worklist: WorklistReader,
    output_path: Path,
    *,
    generated_at: _dt.datetime | None = None,
) -> Path:
    """Write a triage manifest to ``output_path``.

    - Reads all classified units from ``worklist`` via ``get_triage_units()``.
    - Renders them grouped by classification, biggest first within group.
    - Sensitive units are flagged with ⚠ and a "look carefully" note.
    - Duplicate groups are paired into one section with copy-list checkboxes.

    Idempotent: same input + fixed ``generated_at`` produces byte-identical
    output. Atomic write (temp + rename) so a crash never leaves a half-file.
    """
    units = list(worklist.get_triage_units())
    text = render_triage_markdown(units, generated_at=generated_at)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(output_path)
    return output_path


# ---------------------------------------------------------------------------
# CLI registration — typer command.
# ---------------------------------------------------------------------------


def register_triage_command(app) -> None:  # pragma: no cover - thin wrapper
    """Register the ``triage`` command on a typer app."""
    import typer

    from . import platform as _plat

    @app.command("triage")
    def triage_cmd(
        system_dir: Path = typer.Option(
            None,
            "--system-dir",
            "--output-dir",
            help="Where cabinet writes triage.md. Defaults to ~/cabinet/.",
        ),
    ) -> None:
        """Generate the human-readable triage manifest."""
        target_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        from .worklist import Worklist

        db_path = target_dir / "worklist.db"
        if not db_path.is_file():
            typer.echo(
                f"error: no worklist at {db_path}. Run 'cabinet scan' + 'cabinet classify' first.",
                err=True,
            )
            raise typer.Exit(code=2)
        worklist = Worklist(db_path)
        try:
            out = write_triage_report(worklist, target_dir / "triage.md")
        finally:
            worklist.close()
        typer.echo(f"Wrote triage manifest: {out}")

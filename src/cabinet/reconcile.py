"""Phase D — parse the user-marked-up triage.md back into structured decisions.

The user fills in checkboxes in triage.md. This module reads that file and
returns a list of ``Decision`` records. Validation is strict:

- Each per-unit section must have exactly ONE checked checkbox.
- Each dedupe group must have exactly ONE keep marked.
- Anything else surfaces as a ``ReconcileError`` with the line number.

This is the boundary where user intent crosses into machine action — make it
loud when ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .triage import (
    ACTION_ARCHIVE,
    ACTION_DEDUPE,
    ACTION_KEEP,
    ACTION_REVIEW_LATER,
    ACTION_TRASH,
    ALL_ACTIONS,
)

# Action strings as they appear in the file. ``ALL_ACTIONS`` already has them
# verbatim; we map back to canonical short codes here.
_ACTION_TO_CODE: dict[str, str] = {
    ACTION_KEEP: "keep",
    ACTION_ARCHIVE: "archive",
    ACTION_DEDUPE: "dedupe",
    ACTION_REVIEW_LATER: "review-later",
    ACTION_TRASH: "trash",
}

# Match a per-unit header. We tolerate the optional ⚠ marker.
_HEADER_RE = re.compile(r"^##\s+(?:⚠\s+)?(?P<path>/.+?)\s*$")
# Match a dedupe group header.
_DEDUPE_HEADER_RE = re.compile(r"^##\s+DEDUPE GROUP\s+`(?P<gid>[^`]+)`")
# Match a checkbox line.
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<rest>.+?)\s*$")
# Match an "archive" choice with a destination arrow.
_ARCHIVE_DEST_RE = re.compile(r"^archive\s*→\s*(?P<dest>.+?)\s*(?:\*\(suggested\)\*\s*)?$")
# Match the dedupe-group "keep" line: `- [ ] keep: \`/path\``.
_DEDUPE_KEEP_RE = re.compile(r"^keep:\s*`(?P<path>[^`]+)`\s*(?:\*\(suggested\)\*\s*)?$")


@dataclass(frozen=True, slots=True)
class Decision:
    """A user decision parsed out of triage.md.

    For non-dedupe actions: ``unit_path`` is the unit; ``target`` is the
    optional archive destination.

    For dedupe: each duplicate copy gets its own Decision. The chosen-keep
    copy gets ``action="keep"``; the rest get ``action="dedupe"`` with
    ``target`` set to the kept copy's path so the planner knows which is canonical.
    """

    unit_path: str
    action: str  # "keep" | "archive" | "dedupe" | "trash" | "review-later"
    target: str | None = None  # archive dest for archive; canonical path for dedupe
    dedupe_group: str | None = None

    def to_dict(self) -> dict:
        return {
            "unit_path": self.unit_path,
            "action": self.action,
            "target": self.target,
            "dedupe_group": self.dedupe_group,
        }


@dataclass(frozen=True, slots=True)
class ReconcileError:
    line: int
    section: str
    message: str

    def __str__(self) -> str:
        return f"[line {self.line}] {self.section}: {self.message}"


class ReconcileException(Exception):
    """Raised when the triage.md cannot be cleanly parsed."""

    def __init__(self, errors: Sequence[ReconcileError]):
        self.errors = tuple(errors)
        super().__init__("\n".join(str(e) for e in errors))


class WorklistWriter(Protocol):
    """Minimal interface reconcile needs to persist decisions."""

    def write_decisions(self, decisions: Sequence[Decision]) -> None:
        ...


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    """Internal mutable section accumulator while scanning the file."""

    kind: str  # "unit" | "dedupe" | "skip"
    line_no: int  # 1-indexed line where the header appeared
    label: str  # path or dedupe-group id
    checkboxes: list[tuple[int, bool, str]] = field(default_factory=list)
    # Each tuple: (line_no, is_checked, rest_of_line)


def _split_sections(text: str) -> list[_Section]:
    """Split a triage.md into sections — one per ``## ...`` header."""
    sections: list[_Section] = []
    current: _Section | None = None

    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\n")
        m_unit = _HEADER_RE.match(line)
        m_ded = _DEDUPE_HEADER_RE.match(line)
        if m_ded is not None:
            if current is not None:
                sections.append(current)
            current = _Section(kind="dedupe", line_no=idx, label=m_ded.group("gid"))
            continue
        if m_unit is not None:
            if current is not None:
                sections.append(current)
            current = _Section(kind="unit", line_no=idx, label=m_unit.group("path"))
            continue
        # Top-level "##" headers we don't recognize → close current section, ignore.
        if line.startswith("## "):
            if current is not None:
                sections.append(current)
            current = _Section(kind="skip", line_no=idx, label=line)
            continue
        # Other "#" headers (level 1, 3+) just bracket — close any open section.
        if line.startswith("# ") or line.startswith("### "):
            if current is not None:
                sections.append(current)
                current = None
            continue
        # Within a section: pick up checkboxes.
        if current is not None and current.kind != "skip":
            cb = _CHECKBOX_RE.match(line)
            if cb:
                checked = cb.group("mark") in ("x", "X")
                rest = cb.group("rest").strip()
                current.checkboxes.append((idx, checked, rest))
    if current is not None:
        sections.append(current)
    return sections


def _classify_action_line(rest: str) -> tuple[str, str | None] | None:
    """Match one of the per-unit action strings. Returns (code, target)."""
    # Try archive-with-dest first.
    m = _ARCHIVE_DEST_RE.match(rest)
    if m:
        return "archive", m.group("dest")
    # Strip a trailing "*(suggested)*" hint if present.
    cleaned = re.sub(r"\s*\*\(suggested\)\*\s*$", "", rest).strip()
    # Direct match against ALL_ACTIONS strings.
    for action in ALL_ACTIONS:
        if cleaned == action:
            return _ACTION_TO_CODE[action], None
    # Bare "archive" with no dest is also acceptable.
    if cleaned == "archive":
        return "archive", None
    return None


def _decisions_from_unit_section(s: _Section) -> tuple[list[Decision], list[ReconcileError]]:
    """A per-unit section must have exactly one checked checkbox among the actions."""
    checked: list[tuple[int, str, str | None]] = []  # (line, code, target)
    for line_no, is_checked, rest in s.checkboxes:
        parsed = _classify_action_line(rest)
        if parsed is None:
            # Not an action checkbox — ignore (could be malformed but
            # allowing junk reduces parse-fragility).
            continue
        if is_checked:
            checked.append((line_no, parsed[0], parsed[1]))

    if len(checked) == 0:
        return [], [
            ReconcileError(
                line=s.line_no,
                section=s.label,
                message="no checkbox marked — pick exactly one action.",
            )
        ]
    if len(checked) > 1:
        marked = ", ".join(c[1] for c in checked)
        return [], [
            ReconcileError(
                line=checked[0][0],
                section=s.label,
                message=f"{len(checked)} checkboxes marked ({marked}); exactly one required.",
            )
        ]
    line_no, code, target = checked[0]
    return [Decision(unit_path=s.label, action=code, target=target)], []


def _decisions_from_dedupe_section(s: _Section) -> tuple[list[Decision], list[ReconcileError]]:
    """Dedupe section: list of `keep: <path>` checkboxes; exactly one is checked."""
    candidates: list[tuple[int, bool, str]] = []
    for line_no, is_checked, rest in s.checkboxes:
        m = _DEDUPE_KEEP_RE.match(rest)
        if m is None:
            continue
        candidates.append((line_no, is_checked, m.group("path")))

    if not candidates:
        return [], [
            ReconcileError(
                line=s.line_no,
                section=f"dedupe `{s.label}`",
                message="no `keep: <path>` lines found in dedupe section.",
            )
        ]
    checked = [c for c in candidates if c[1]]
    if len(checked) == 0:
        return [], [
            ReconcileError(
                line=s.line_no,
                section=f"dedupe `{s.label}`",
                message="no copy marked keep — pick exactly one to keep.",
            )
        ]
    if len(checked) > 1:
        return [], [
            ReconcileError(
                line=checked[0][0],
                section=f"dedupe `{s.label}`",
                message=f"{len(checked)} copies marked keep; exactly one required.",
            )
        ]
    keep_path = checked[0][2]
    decisions: list[Decision] = []
    for _, _, path in candidates:
        if path == keep_path:
            decisions.append(
                Decision(
                    unit_path=path,
                    action="keep",
                    target=None,
                    dedupe_group=s.label,
                )
            )
        else:
            decisions.append(
                Decision(
                    unit_path=path,
                    action="dedupe",
                    target=keep_path,
                    dedupe_group=s.label,
                )
            )
    return decisions, []


def parse_triage(triage_path: Path) -> list[Decision]:
    """Parse a marked-up triage.md back into structured decisions.

    Raises ``ReconcileException`` if any section is ambiguous (0 or >1 marks).
    """
    text = Path(triage_path).read_text(encoding="utf-8")
    sections = _split_sections(text)

    decisions: list[Decision] = []
    errors: list[ReconcileError] = []

    for s in sections:
        if s.kind == "unit":
            d, e = _decisions_from_unit_section(s)
            decisions.extend(d)
            errors.extend(e)
        elif s.kind == "dedupe":
            d, e = _decisions_from_dedupe_section(s)
            decisions.extend(d)
            errors.extend(e)
        # skip sections (e.g. "How to use") have no decisions.

    if errors:
        raise ReconcileException(errors)
    return decisions


def persist_decisions(worklist: WorklistWriter, decisions: Sequence[Decision]) -> None:
    """Hand the parsed decisions to the worklist for storage."""
    worklist.write_decisions(list(decisions))


# ---------------------------------------------------------------------------
# CLI registration.
# ---------------------------------------------------------------------------


def register_reconcile_command(app) -> None:  # pragma: no cover - thin wrapper
    """Register the ``reconcile`` command on a typer app."""
    import typer

    from . import platform as _plat

    @app.command("reconcile")
    def reconcile_cmd(
        system_dir: Path = typer.Option(
            None,
            "--system-dir",
            "--output-dir",
            help="Cabinet system dir (where triage.md lives). Defaults to ~/cabinet/.",
        ),
        triage: Path = typer.Option(
            None,
            "--triage",
            help="Explicit path to a triage.md. Overrides --system-dir.",
        ),
    ) -> None:
        """Parse the marked-up triage.md into structured decisions."""
        if triage is not None:
            triage_path = Path(triage)
        else:
            sys_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
            triage_path = sys_dir / "triage.md"
        try:
            decisions = parse_triage(triage_path)
        except ReconcileException as exc:
            typer.echo("Triage parse failed:")
            for err in exc.errors:
                typer.echo(f"  {err}")
            raise typer.Exit(code=2)
        # Persist via the worklist module the CLI agent wires up.
        from .worklist import Worklist

        sys_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
        worklist = Worklist(sys_dir / 'worklist.db')
        persist_decisions(worklist, decisions)
        typer.echo(f"Parsed {len(decisions)} decisions.")

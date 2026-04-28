"""Phase E — actions → reversible action manifest.

The planner is the last stop before any filesystem writes. It produces an
``ActionPlan`` — pure data — that the apply step can execute, render, or
inspect. The plan is deterministic: same input → byte-identical JSON.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

# Schema version — bump when ActionPlan layout changes incompatibly.
PLAN_SCHEMA_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class Action:
    """One filesystem action.

    Ops:
        "move":  rename / relocate ``source`` to ``dest``.
        "skip":  no-op (kept-classified or review-later units; emitted for visibility).

    ``reason`` is human-readable; ``evidence_unit_id`` ties this back to the
    triage decision that justified it.
    """

    op: str
    source: str
    dest: str | None
    reason: str
    evidence_unit_id: str

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "source": self.source,
            "dest": self.dest,
            "reason": self.reason,
            "evidence_unit_id": self.evidence_unit_id,
        }


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """An ordered list of actions plus metadata. Pure data — no side effects."""

    schema_version: int
    generated_at: str  # ISO 8601 UTC
    archive_root: str
    review_pile: str
    actions: tuple[Action, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "archive_root": self.archive_root,
            "review_pile": self.review_pile,
            "actions": [a.to_dict() for a in self.actions],
        }


class WorklistDecisionsReader(Protocol):
    """Minimal interface needed from the worklist."""

    def get_decisions(self) -> Sequence:
        # Returns list of reconcile.Decision (duck-typed: .unit_path, .action, .target, .dedupe_group)
        ...


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _archive_dest_for(unit_path: str, archive_root: Path, target_hint: str | None) -> str:
    """Compute the archive destination for a unit.

    Priority:
      1. User-provided hint (from triage `archive → <dest>`).
      2. Default: ``<archive_root>/<basename>``.

    Hints are expanded (~ etc.) and resolved relative to home if not absolute.
    """
    if target_hint:
        p = Path(target_hint).expanduser()
        if not p.is_absolute():
            p = archive_root / p
        return str(p)
    return str(archive_root / Path(unit_path).name)


def _hash_prefix(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _dedupe_dest_for(unit_path: str, review_pile: Path, dedupe_group: str | None) -> str:
    """Where to put a non-keep duplicate copy.

    Layout: ``<review_pile>/dedupe/<group-hash>/<basename>``.
    Group hash makes paths short + collision-resistant.
    """
    gid = dedupe_group or "ungrouped"
    bucket = _hash_prefix(gid)
    return str(review_pile / "dedupe" / bucket / Path(unit_path).name)


def _trash_dest_for(unit_path: str, review_pile: Path) -> str:
    """Where to put a trashed item — flat under review_pile/trashed/.

    Layout: ``<review_pile>/trashed/<sha8>_<basename>``. The 8-char hash of
    the full source path disambiguates files that share a basename but live
    in different parents (e.g. a ``README.md`` from each of three projects).
    The basename is preserved as a suffix so the user can still recognise
    what was trashed without grepping a manifest.
    """
    sha8 = _hash_prefix(unit_path, n=8)
    name = Path(unit_path).name or "unnamed"
    return str(review_pile / "trashed" / f"{sha8}_{name}")


def build_plan(
    worklist: WorklistDecisionsReader,
    *,
    archive_root: Path,
    review_pile: Path,
    generated_at: _dt.datetime | None = None,
) -> ActionPlan:
    """Build an ActionPlan from approved decisions in the worklist.

    Pure: never touches the filesystem. The plan is sorted deterministically
    so re-running with the same decisions yields identical JSON.
    """
    if generated_at is None:
        generated_at = _dt.datetime.utcnow()

    archive_root = Path(archive_root).expanduser().resolve()
    review_pile = Path(review_pile).expanduser().resolve()

    decisions = list(worklist.get_decisions())
    actions: list[Action] = []

    for d in decisions:
        action_code = getattr(d, "action")
        unit_path = getattr(d, "unit_path")
        target = getattr(d, "target", None)
        dedupe_group = getattr(d, "dedupe_group", None)

        if action_code == "keep":
            actions.append(
                Action(
                    op="skip",
                    source=unit_path,
                    dest=None,
                    reason="kept by user decision",
                    evidence_unit_id=unit_path,
                )
            )
        elif action_code == "review-later":
            actions.append(
                Action(
                    op="skip",
                    source=unit_path,
                    dest=None,
                    reason="review later — explicit user skip",
                    evidence_unit_id=unit_path,
                )
            )
        elif action_code == "archive":
            dest = _archive_dest_for(unit_path, archive_root, target)
            actions.append(
                Action(
                    op="move",
                    source=unit_path,
                    dest=dest,
                    reason="archive by user decision",
                    evidence_unit_id=unit_path,
                )
            )
        elif action_code == "trash":
            dest = _trash_dest_for(unit_path, review_pile)
            actions.append(
                Action(
                    op="move",
                    source=unit_path,
                    dest=dest,
                    reason="trash → review pile (reversible)",
                    evidence_unit_id=unit_path,
                )
            )
        elif action_code == "dedupe":
            # The kept copy stays in place; non-keeps move to dedupe pile.
            dest = _dedupe_dest_for(unit_path, review_pile, dedupe_group)
            keep_target = target or "(unknown keep)"
            actions.append(
                Action(
                    op="move",
                    source=unit_path,
                    dest=dest,
                    reason=f"dedupe — keep {keep_target}",
                    evidence_unit_id=unit_path,
                )
            )
        else:
            # Unknown action codes become explicit skips with a loud reason
            # rather than silent drops.
            actions.append(
                Action(
                    op="skip",
                    source=unit_path,
                    dest=None,
                    reason=f"unknown action `{action_code}` — left untouched",
                    evidence_unit_id=unit_path,
                )
            )

    # Deterministic ordering: by (op, source). "move" before "skip" so the
    # interesting actions come first; alphabetical within each op.
    op_rank = {"move": 0, "skip": 1}
    actions.sort(key=lambda a: (op_rank.get(a.op, 9), a.source))

    return ActionPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        generated_at=generated_at.replace(microsecond=0).isoformat() + "Z",
        archive_root=str(archive_root),
        review_pile=str(review_pile),
        actions=tuple(actions),
    )


# ---------------------------------------------------------------------------
# Rendering & persistence
# ---------------------------------------------------------------------------


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ("KB", "MB", "GB", "TB")
    value = float(num_bytes)
    unit_idx = -1
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024
        unit_idx += 1
    return f"{value:.1f} {units[max(unit_idx, 0)]}"


def render_plan(plan: ActionPlan, *, top_n: int = 10) -> str:
    """Human-readable summary of a plan.

    Counts moves, total bytes that would be moved (best effort — uses on-disk
    size at render time), and shows the top-N largest moves.
    """
    moves = [a for a in plan.actions if a.op == "move"]
    skips = [a for a in plan.actions if a.op == "skip"]

    sized: list[tuple[int, Action]] = []
    total_bytes = 0
    for a in moves:
        try:
            sz = Path(a.source).stat().st_size if Path(a.source).is_file() else _dir_size(Path(a.source))
        except OSError:
            sz = 0
        sized.append((sz, a))
        total_bytes += sz

    sized.sort(key=lambda t: -t[0])

    lines = [
        "# Plan summary",
        f"- Schema version: {plan.schema_version}",
        f"- Generated at: {plan.generated_at}",
        f"- Archive root: {plan.archive_root}",
        f"- Review pile:  {plan.review_pile}",
        f"- Total actions: {len(plan.actions)} ({len(moves)} moves, {len(skips)} skips)",
        f"- Total bytes moved: {_format_size(total_bytes)}",
        "",
        f"## Top {top_n} largest moves",
        "",
    ]
    for sz, a in sized[:top_n]:
        lines.append(f"- [{_format_size(sz):>10}] {a.source} → {a.dest}")
    if not sized:
        lines.append("(none)")
    return "\n".join(lines) + "\n"


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for entry in p.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def write_plan(plan: ActionPlan, output_path: Path) -> Path:
    """Persist the plan to a deterministic JSON file (sorted keys, schema-versioned)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.to_dict()
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(output_path)
    return output_path


def load_plan(path: Path) -> ActionPlan:
    """Reload a plan from disk. Round-trips with ``write_plan``."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = tuple(
        Action(
            op=a["op"],
            source=a["source"],
            dest=a.get("dest"),
            reason=a["reason"],
            evidence_unit_id=a["evidence_unit_id"],
        )
        for a in payload.get("actions", [])
    )
    return ActionPlan(
        schema_version=payload.get("schema_version", PLAN_SCHEMA_VERSION),
        generated_at=payload["generated_at"],
        archive_root=payload["archive_root"],
        review_pile=payload["review_pile"],
        actions=actions,
    )


# ---------------------------------------------------------------------------
# CLI registration.
# ---------------------------------------------------------------------------


def register_plan_command(app) -> None:  # pragma: no cover - thin wrapper
    """Register the ``plan`` command on a typer app."""
    import typer

    from . import platform as _plat

    @app.command("plan")
    def plan_cmd(
        system_dir: Path = typer.Option(
            None,
            "--system-dir",
            "--output-dir",
            help="Cabinet system dir. Defaults to ~/cabinet/.",
        ),
        archive_root: Path = typer.Option(
            None,
            "--archive-root",
            help="Where archive moves go. Defaults to ~/cabinet-archive/.",
        ),
        review_pile: Path = typer.Option(
            None,
            "--review-pile",
            help="Where trash/dedupe moves go. Defaults to ~/cabinet-review-pile/.",
        ),
    ) -> None:
        """Generate the reversible action plan from approved decisions."""
        sys_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
        sys_dir.mkdir(parents=True, exist_ok=True)
        a_root = Path(archive_root) if archive_root else _plat.default_archive_root()
        r_pile = Path(review_pile) if review_pile else _plat.default_review_pile()

        from .worklist import Worklist

        worklist = Worklist(sys_dir / 'worklist.db')
        plan = build_plan(worklist, archive_root=a_root, review_pile=r_pile)
        ts = plan.generated_at.replace(":", "").replace("-", "").replace("Z", "")
        out = sys_dir / f"plan-{ts}.json"
        write_plan(plan, out)
        typer.echo(render_plan(plan))
        typer.echo(f"Plan written: {out}")

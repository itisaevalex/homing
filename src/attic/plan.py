"""attic plan — cold-data selection → an ArchivePlan (pure data).

Modeled on ``cabinet.planner``: dataclasses with ``to_dict`` plus
``load_plan``/``write_plan`` for deterministic JSON round-trips. The plan is the
last read-only stop before any upload — it selects which paths are cold enough
to evict (by size and age) and assigns each a collision-resistant remote key.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PLAN_SCHEMA_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class ArchiveAction:
    """One archive action — push ``source`` to ``remote_key``."""

    op: str  # "push"
    source: str
    remote_key: str
    reason: str
    is_dir: bool

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "source": self.source,
            "remote_key": self.remote_key,
            "reason": self.reason,
            "is_dir": self.is_dir,
        }


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """An ordered list of archive actions plus metadata. Pure data."""

    schema_version: int
    generated_at: str  # ISO 8601 UTC
    remote: str
    actions: tuple[ArchiveAction, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "remote": self.remote,
            "actions": [a.to_dict() for a in self.actions],
        }


# ---------------------------------------------------------------------------
# Sizing / age helpers
# ---------------------------------------------------------------------------


def _dir_size(p: Path) -> int:
    total = 0
    for entry in p.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _path_size(p: Path) -> int:
    if p.is_dir() and not p.is_symlink():
        return _dir_size(p)
    try:
        return p.lstat().st_size
    except OSError:
        return 0


def _age_days(p: Path, *, now: float) -> float:
    """Age in days from mtime. For dirs, the newest file mtime in the tree."""
    try:
        newest = p.lstat().st_mtime
    except OSError:
        return 0.0
    if p.is_dir() and not p.is_symlink():
        for entry in p.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
    return max(0.0, (now - newest) / 86400.0)


def _hash_prefix(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _remote_key_for(abs_path: str) -> str:
    """Collision-resistant remote key: ``<basename>-<sha12(abspath)>``.

    The basename keeps keys human-recognisable; the abs-path hash disambiguates
    two sources that share a basename (e.g. ``logs/`` from two projects).
    """
    name = Path(abs_path).name or "unnamed"
    return f"{name}-{_hash_prefix(abs_path)}"


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def build_plan_from_paths(
    paths: Sequence[Path],
    *,
    remote: str,
    min_size: int = 0,
    min_age_days: int = 0,
    generated_at: _dt.datetime | None = None,
    now: float | None = None,
) -> ArchivePlan:
    """Build an ArchivePlan from candidate paths, filtered by size and age.

    Each path is stat'd (recursive size for dirs). Paths smaller than
    ``min_size`` or younger than ``min_age_days`` are dropped. Missing paths are
    dropped too (a plan never references a non-existent source). Output is
    sorted by source for deterministic JSON.
    """
    if generated_at is None:
        generated_at = _dt.datetime.utcnow()
    if now is None:
        now = time.time()

    actions: list[ArchiveAction] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not os.path.lexists(p):
            continue
        abs_path = str(p.resolve())
        is_dir = p.is_dir() and not p.is_symlink()
        size = _path_size(p)
        if size < min_size:
            continue
        if _age_days(p, now=now) < min_age_days:
            continue
        actions.append(
            ArchiveAction(
                op="push",
                source=abs_path,
                remote_key=_remote_key_for(abs_path),
                reason=(
                    f"cold: size={size}B, age>={min_age_days}d"
                    if min_age_days
                    else f"selected: size={size}B"
                ),
                is_dir=is_dir,
            )
        )

    actions.sort(key=lambda a: a.source)
    return ArchivePlan(
        schema_version=PLAN_SCHEMA_VERSION,
        generated_at=generated_at.replace(microsecond=0).isoformat() + "Z",
        remote=remote,
        actions=tuple(actions),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_plan(plan: ArchivePlan, output_path: Path) -> Path:
    """Persist the plan to deterministic JSON (sorted keys, schema-versioned)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plan.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(output_path)
    return output_path


def load_plan(path: Path) -> ArchivePlan:
    """Reload a plan from disk. Round-trips with ``write_plan``."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = tuple(
        ArchiveAction(
            op=a["op"],
            source=a["source"],
            remote_key=a["remote_key"],
            reason=a["reason"],
            is_dir=bool(a["is_dir"]),
        )
        for a in payload.get("actions", [])
    )
    return ArchivePlan(
        schema_version=payload.get("schema_version", PLAN_SCHEMA_VERSION),
        generated_at=payload["generated_at"],
        remote=payload["remote"],
        actions=actions,
    )


def render_plan(plan: ArchivePlan) -> str:
    """Human-readable plan summary."""
    lines = [
        "# attic plan",
        f"- Schema version: {plan.schema_version}",
        f"- Generated at: {plan.generated_at}",
        f"- Remote: {plan.remote}",
        f"- Actions: {len(plan.actions)}",
        "",
    ]
    for a in plan.actions:
        kind = "dir " if a.is_dir else "file"
        lines.append(f"- [{kind}] {a.source} → {a.remote_key}")
    if not plan.actions:
        lines.append("(no paths matched the filters)")
    return "\n".join(lines) + "\n"

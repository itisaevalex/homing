"""Phase A — two-pass walk of ``$HOME``.

This module is intentionally pure: it reads the filesystem and returns
data structures. It never writes, deletes, or moves anything in the
source tree (see ``CLAUDE.md`` § "Source is sacred").

Two passes over ``$HOME``:

1. **Project hunter** — depth-bounded walk that descends into every
   directory not on the configured prune list, looking for files that
   signal "this dir is a project root" (``.git``, ``package.json``,
   ``pyproject.toml``, …). Once a project root is identified the walker
   does not recurse further into it; nested projects are picked up
   independently if their parent itself is not a project.
2. **Place classifier** — depth-2 walk that classifies each top-level
   ``$HOME`` entry (and known second-level entries under
   ``config-and-data-mixed`` parents like ``~/.local``) using the
   ``known_places`` map.

Output is sorted deterministically by path. Re-runs against an
unchanged tree produce identical lists, satisfying the idempotency
requirement.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Categories whose top-level dir holds a mix of config and data and where the
# *interesting* classifications live one level deeper (e.g. ~/.local/share/*).
# We do a shallow second-level enumeration for these and emit each child as
# its own place if we can classify it; otherwise the parent stands.
_MIXED_CONTAINER_CATEGORIES = frozenset({"config-and-data-mixed"})


def enumerate_home(home: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Walk ``home`` and return a structured enumeration.

    Args:
        home: Filesystem root to walk (typically ``$HOME``).
        config: Parsed platform config (see ``config/platforms/<os>.yaml``).

    Returns:
        A dict with keys ``generated_at``, ``platform``, ``projects``,
        ``places``, ``skipped``, and ``errors``. All list values are
        sorted by path so the output is deterministic.
    """
    home = Path(home).resolve()
    project_hunter_cfg = config.get("project_hunter", {}) or {}
    place_cfg = config.get("place_classifier", {}) or {}
    platform_name = config.get("platform", "unknown")

    prune_directories = frozenset(project_hunter_cfg.get("prune_directories", []) or [])
    prune_paths = frozenset(
        _normalise_relative(p) for p in (project_hunter_cfg.get("prune_paths", []) or [])
    )
    project_signals = list(project_hunter_cfg.get("project_signals", []) or [])
    project_signals_set = frozenset(project_signals)
    max_depth = int(project_hunter_cfg.get("max_depth", 8))

    known_places: dict[str, str] = dict(place_cfg.get("known_places", {}) or {})

    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    projects = _walk_projects(
        home=home,
        prune_directories=prune_directories,
        prune_paths=prune_paths,
        project_signals=project_signals_set,
        max_depth=max_depth,
        errors=errors,
        skipped=skipped,
    )

    places = _classify_places(
        home=home,
        known_places=known_places,
        errors=errors,
    )

    projects.sort(key=lambda p: p["path"])
    places.sort(key=lambda p: p["path"])
    skipped.sort(key=lambda s: s["path"])
    errors.sort(key=lambda e: (e.get("path", ""), e.get("message", "")))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform_name,
        "projects": projects,
        "places": places,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Pass 1: project hunter
# ---------------------------------------------------------------------------


def _walk_projects(
    home: Path,
    prune_directories: frozenset[str],
    prune_paths: frozenset[str],
    project_signals: frozenset[str],
    max_depth: int,
    errors: list[dict[str, str]],
    skipped: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Project hunter — find every directory that contains a project signal."""
    found: list[dict[str, Any]] = []

    # We use an explicit stack so we can tag depth and prune precisely. Each
    # entry is (Path, depth_from_home).
    stack: list[tuple[Path, int]] = [(home, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except PermissionError as exc:
            errors.append({"path": str(current), "message": f"permission denied: {exc}"})
            continue
        except OSError as exc:
            errors.append({"path": str(current), "message": f"os error: {exc}"})
            continue

        # First, scan child names to identify project signals at THIS level.
        names_here = {e.name for e in entries}
        signals_here = sorted(names_here & project_signals)
        if signals_here and current != home:
            # current is a project root.
            found.append(_build_project_record(current, signals_here, errors))
            # Do not descend further into a project root — nested projects
            # within it are intentionally not enumerated separately. The
            # AGENT.md for the parent project is the right place to mention
            # them.
            continue

        if depth >= max_depth:
            continue

        for entry in entries:
            if not _is_directory_safe(entry, errors):
                continue
            child = Path(entry.path)
            rel = _relative_to(home, child)
            # Prune if the child name or relative path is on the prune list.
            if entry.name in prune_directories or rel in prune_paths:
                skipped.append(
                    {
                        "path": str(child),
                        "reason": "pruned by config",
                        "rule": entry.name if entry.name in prune_directories else rel,
                    }
                )
                continue
            stack.append((child, depth + 1))

    return found


def _build_project_record(
    path: Path,
    signals_found: list[str],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Materialise a ProjectCandidate dict for ``path``."""
    size_bytes, last_mtime = _shallow_stats(path, errors)
    return {
        "path": str(path),
        "signals_found": signals_found,
        "size_bytes": size_bytes,
        "last_mtime": last_mtime,
    }


# ---------------------------------------------------------------------------
# Pass 2: place classifier
# ---------------------------------------------------------------------------


def _classify_places(
    home: Path,
    known_places: dict[str, str],
    errors: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Walk depth ≤ 2 of ``home`` and emit every dir we recognise as a place.

    The first level uses ``known_places`` directly. Mixed-container parents
    (e.g. ``~/.local`` → ``config-and-data-mixed``) are also walked one level
    deeper; second-level children whose names appear in ``known_places`` are
    emitted alongside the parent.
    """
    out: list[dict[str, Any]] = []

    try:
        first_level = list(os.scandir(home))
    except PermissionError as exc:
        errors.append({"path": str(home), "message": f"permission denied: {exc}"})
        return out
    except OSError as exc:
        errors.append({"path": str(home), "message": f"os error: {exc}"})
        return out

    for entry in first_level:
        category = known_places.get(entry.name)
        if category is None:
            continue
        path = Path(entry.path)
        out.append(_build_place_record(path, category, errors))
        if category in _MIXED_CONTAINER_CATEGORIES and entry.is_dir(follow_symlinks=False):
            out.extend(_classify_second_level(path, known_places, errors))

    return out


def _classify_second_level(
    parent: Path,
    known_places: dict[str, str],
    errors: list[dict[str, str]],
) -> Iterable[dict[str, Any]]:
    """Emit known-named second-level children of ``parent`` as places."""
    try:
        children = list(os.scandir(parent))
    except PermissionError as exc:
        errors.append({"path": str(parent), "message": f"permission denied: {exc}"})
        return
    except OSError as exc:
        errors.append({"path": str(parent), "message": f"os error: {exc}"})
        return

    for entry in children:
        category = known_places.get(entry.name)
        if category is None:
            continue
        yield _build_place_record(Path(entry.path), category, errors)


def _build_place_record(
    path: Path,
    category: str,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    """Materialise a Place dict for ``path``."""
    size_bytes, last_mtime = _shallow_stats(path, errors)
    return {
        "path": str(path),
        "category": category,
        "size_bytes": size_bytes,
        "last_mtime": last_mtime,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shallow_stats(path: Path, errors: list[dict[str, str]]) -> tuple[int, float]:
    """Return ``(size_bytes, last_mtime)`` from a single ``stat`` call.

    We deliberately do not recurse to compute total size; on a real ``$HOME``
    that would dominate the runtime. ``size_bytes`` here is the size of the
    directory inode itself when the path is a directory. Callers that care
    about full tree size should compute it lazily later.
    """
    try:
        st = path.stat()
    except (PermissionError, FileNotFoundError, OSError) as exc:
        errors.append({"path": str(path), "message": f"stat failed: {exc}"})
        return 0, 0.0
    return int(st.st_size), float(st.st_mtime)


def _is_directory_safe(entry: os.DirEntry[str], errors: list[dict[str, str]]) -> bool:
    """``entry.is_dir(follow_symlinks=False)`` with error capture."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError as exc:
        errors.append({"path": entry.path, "message": f"is_dir failed: {exc}"})
        return False


def _relative_to(home: Path, path: Path) -> str:
    """Return ``path`` relative to ``home`` as a forward-slash string."""
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    return rel.as_posix()


def _normalise_relative(p: str) -> str:
    """Normalise a configured ``prune_paths`` entry to forward-slash form."""
    return Path(p).as_posix()

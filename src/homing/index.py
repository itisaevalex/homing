"""Phase G — aggregate AGENT.md / PLACE.md frontmatter into ``index.json``.

Walks ``<system_dir>/projects/<name>/AGENT.md`` and
``<system_dir>/places/<name>/PLACE.md`` files, parses the YAML frontmatter,
computes a SHA-256 of each file, and merges in any rule findings from the
worklist. The result is a single JSON document agents can read to answer
"what's on this machine" without re-walking the source tree.

Determinism: both project and place lists are sorted by name, every dict
key is sorted, and re-running on unchanged input produces byte-identical
output (modulo the ``generated_at`` timestamp). Missing manifests are
tolerated: the index still lists the unit by name (sourced from the
worklist) with empty body fields and a warning attached at the top.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from homing.worklist import Worklist

SCHEMA_VERSION = "v2"


def build_index(worklist: Worklist | None, manifests_root: Path) -> dict[str, Any]:
    """Aggregate manifests under ``manifests_root`` into a single dict.

    Args:
        worklist: Open :class:`Worklist` to pull rule findings from. May be
            ``None`` for tests that just want the filesystem aggregation.
        manifests_root: Directory containing ``projects/`` and ``places/``
            subdirs (typically ``<system_dir>``).

    Returns:
        Dict matching the schema described in the module docstring.
    """
    manifests_root = Path(manifests_root)
    warnings: list[str] = []

    projects = _collect_units(
        kind="project",
        root=manifests_root / "projects",
        manifest_filename="AGENT.md",
        worklist=worklist,
        warnings=warnings,
    )
    places = _collect_units(
        kind="place",
        root=manifests_root / "places",
        manifest_filename="PLACE.md",
        worklist=worklist,
        warnings=warnings,
    )

    # Merge in worklist-only units that have no manifest written yet, so
    # `homing query` can still surface them.
    if worklist is not None:
        seen_project_names = {p["name"] for p in projects}
        seen_place_names = {p["name"] for p in places}
        for unit in _all_units_from_worklist(worklist):
            if unit["kind"] == "project" and unit["name"] not in seen_project_names:
                projects.append(_unit_stub_from_worklist(unit, worklist, "AGENT.md"))
            elif unit["kind"] == "place" and unit["name"] not in seen_place_names:
                places.append(_unit_stub_from_worklist(unit, worklist, "PLACE.md"))

    projects.sort(key=lambda r: r["name"])
    places.sort(key=lambda r: r["name"])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "project_count": len(projects),
        "place_count": len(places),
        "projects": [_sort_dict(p) for p in projects],
        "places": [_sort_dict(p) for p in places],
        "warnings": sorted(warnings),
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _collect_units(
    *,
    kind: str,
    root: Path,
    manifest_filename: str,
    worklist: Worklist | None,
    warnings: list[str],
) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        manifest_path = child / manifest_filename
        if not manifest_path.is_file():
            warnings.append(f"missing {manifest_filename} for {kind} {child.name!r}")
            record = _unit_stub(child.name, kind, manifest_path, manifest_filename)
        else:
            record = _parse_manifest(child.name, kind, manifest_path, manifest_filename, warnings)

        if worklist is not None:
            record["rule_findings"] = _findings_for(worklist, child.name)
        else:
            record["rule_findings"] = []
        out.append(record)
    return out


def _parse_manifest(
    name: str,
    kind: str,
    manifest_path: Path,
    manifest_filename: str,
    warnings: list[str],
) -> dict[str, Any]:
    try:
        post = frontmatter.load(manifest_path)
        meta = dict(post.metadata or {})
    except Exception as exc:
        warnings.append(f"failed to parse frontmatter for {name!r}: {type(exc).__name__}: {exc}")
        meta = {}

    sha = _sha256_file(manifest_path)
    base_key = manifest_filename.lower().replace(".md", "")
    record: dict[str, Any] = {
        "name": name,
        "kind": kind,
        f"{base_key}_md_path": str(manifest_path),
        f"{base_key}_md_sha256": sha,
    }
    # Frontmatter fields are merged in. The ``name`` and ``kind`` keys are
    # reserved by the index itself; we tolerate matching values from
    # frontmatter (treat as redundant, not a warning) and reject mismatches.
    reserved = {"name", "kind", f"{base_key}_md_path", f"{base_key}_md_sha256"}
    for k, v in meta.items():
        coerced = _json_safe(v)
        if k in reserved:
            if record[k] != coerced:
                warnings.append(
                    f"frontmatter key {k!r} for {name!r} disagrees with directory-derived "
                    f"value ({coerced!r} vs {record[k]!r}); directory wins"
                )
            continue
        record[k] = coerced
    return record


def _json_safe(value: Any) -> Any:
    """Recursively coerce frontmatter values to JSON-serialisable forms.

    YAML 1.1 auto-parses ISO timestamps into ``datetime`` / ``date`` objects;
    keep them in the manifest body as strings for byte-stable JSON output.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def _unit_stub(
    name: str, kind: str, manifest_path: Path, manifest_filename: str
) -> dict[str, Any]:
    """Stub record when the manifest dir exists but the manifest file does not."""
    base = manifest_filename.lower().replace(".md", "")
    return {
        "name": name,
        "kind": kind,
        f"{base}_md_path": str(manifest_path),
        f"{base}_md_sha256": None,
    }


def _unit_stub_from_worklist(
    unit: dict[str, Any], worklist: Worklist, manifest_filename: str
) -> dict[str, Any]:
    """Stub record when the unit lives only in the worklist (no manifest dir)."""
    base = manifest_filename.lower().replace(".md", "")
    return {
        "name": unit["name"],
        "kind": unit["kind"],
        "path": unit["path"],
        "status": unit["status"],
        f"{base}_md_path": None,
        f"{base}_md_sha256": None,
        "rule_findings": _findings_for(worklist, unit["name"]),
    }


def _findings_for(worklist: Worklist, name: str) -> list[dict[str, Any]]:
    try:
        findings = worklist.findings_for(name)
    except KeyError:
        return []
    # Drop the surrogate IDs and timestamps that vary across runs;
    # index.json is meant to be byte-identical on unchanged input.
    return [
        {
            "rule": f["rule"],
            "confidence": f["confidence"],
            "classifications": f["classifications"],
            "evidence": f["evidence"],
        }
        for f in findings
    ]


def _all_units_from_worklist(worklist: Worklist) -> list[dict[str, Any]]:
    """Pull every unit from the worklist regardless of status, sorted by name."""
    seen: dict[str, dict[str, Any]] = {}
    # Worklist exposes units_by_status; iterate over every valid status
    # so we don't depend on schema details.
    from homing.worklist import VALID_STATUSES

    for status in VALID_STATUSES:
        for u in worklist.units_by_status(status):
            seen[u["name"]] = u
    return sorted(seen.values(), key=lambda u: u["name"])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sort_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively sort dict keys for byte-identical JSON output."""
    out: dict[str, Any] = {}
    for k in sorted(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            out[k] = _sort_dict(v)
        elif isinstance(v, list):
            out[k] = [_sort_dict(item) if isinstance(item, dict) else item for item in v]
        else:
            out[k] = v
    return out

"""Tests for ``homing.index`` — Phase G frontmatter aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homing.index import SCHEMA_VERSION, build_index
from homing.worklist import Worklist


def _write_manifest(path: Path, frontmatter_dict: dict, body: str = "body") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    for k in sorted(frontmatter_dict.keys()):
        v = frontmatter_dict[k]
        if isinstance(v, str):
            fm_lines.append(f"{k}: {v}")
        else:
            fm_lines.append(f"{k}: {json.dumps(v)}")
    fm_lines.append("---")
    fm_lines.append(body)
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")


@pytest.fixture
def system_dir(tmp_path: Path) -> Path:
    """Create a system dir with two projects and one place."""
    _write_manifest(
        tmp_path / "projects" / "alpha" / "AGENT.md",
        {
            "name": "alpha",
            "kind": "project",
            "state": "active",
            "purpose": "demo project A",
            "last_meaningful_activity": "2026-04-01T00:00:00+00:00",
        },
        body="# Alpha\n",
    )
    _write_manifest(
        tmp_path / "projects" / "beta" / "AGENT.md",
        {
            "name": "beta",
            "kind": "project",
            "state": "stale",
            "purpose": "demo project B",
            "last_meaningful_activity": "2024-01-01T00:00:00+00:00",
        },
        body="# Beta\n",
    )
    _write_manifest(
        tmp_path / "places" / "Documents" / "PLACE.md",
        {
            "name": "Documents",
            "kind": "place",
            "category": "personal-data",
        },
        body="# Documents\n",
    )
    return tmp_path


def test_build_index_basic_shape(system_dir: Path) -> None:
    payload = build_index(None, system_dir)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["project_count"] == 2
    assert payload["place_count"] == 1
    assert isinstance(payload["projects"], list)
    assert isinstance(payload["places"], list)
    assert payload["warnings"] == []


def test_projects_sorted_by_name(system_dir: Path) -> None:
    payload = build_index(None, system_dir)
    names = [p["name"] for p in payload["projects"]]
    assert names == sorted(names)
    assert names == ["alpha", "beta"]


def test_frontmatter_fields_propagated(system_dir: Path) -> None:
    payload = build_index(None, system_dir)
    alpha = next(p for p in payload["projects"] if p["name"] == "alpha")
    assert alpha["state"] == "active"
    assert alpha["purpose"] == "demo project A"
    assert "agent_md_path" in alpha
    assert "agent_md_sha256" in alpha
    assert isinstance(alpha["agent_md_sha256"], str) and len(alpha["agent_md_sha256"]) == 64


def test_place_md_aggregated(system_dir: Path) -> None:
    payload = build_index(None, system_dir)
    docs = payload["places"][0]
    assert docs["name"] == "Documents"
    assert docs["category"] == "personal-data"
    assert "place_md_path" in docs
    assert isinstance(docs["place_md_sha256"], str)


def test_idempotent_byte_for_byte_modulo_timestamp(system_dir: Path) -> None:
    a = build_index(None, system_dir)
    b = build_index(None, system_dir)
    a.pop("generated_at")
    b.pop("generated_at")
    # Re-serialise both with sort_keys to confirm byte-equality.
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_missing_manifest_file_emits_warning(tmp_path: Path) -> None:
    # Create a project dir without an AGENT.md.
    (tmp_path / "projects" / "ghost").mkdir(parents=True)
    payload = build_index(None, tmp_path)
    assert payload["project_count"] == 1
    assert any("ghost" in w for w in payload["warnings"])


def test_findings_pulled_from_worklist(system_dir: Path) -> None:
    wl = Worklist(":memory:")
    try:
        wl.add_unit("project", "alpha", "/x/alpha")
        wl.record_finding(
            "alpha",
            rule="is-python-project",
            confidence=1.0,
            classifications={"stack": ["python"]},
            evidence=[("/x/alpha/pyproject.toml", "present")],
        )
        payload = build_index(wl, system_dir)
        alpha = next(p for p in payload["projects"] if p["name"] == "alpha")
        assert alpha["rule_findings"]
        assert alpha["rule_findings"][0]["rule"] == "is-python-project"
    finally:
        wl.close()


def test_empty_system_dir(tmp_path: Path) -> None:
    payload = build_index(None, tmp_path)
    assert payload["project_count"] == 0
    assert payload["place_count"] == 0
    assert payload["projects"] == []
    assert payload["places"] == []


def test_worklist_only_unit_appears_in_index(tmp_path: Path) -> None:
    """A unit that's in the worklist but has no manifest still appears."""
    wl = Worklist(":memory:")
    try:
        wl.add_unit("project", "from-worklist", "/x/wl")
        payload = build_index(wl, tmp_path)
        assert payload["project_count"] == 1
        rec = payload["projects"][0]
        assert rec["name"] == "from-worklist"
        assert rec["agent_md_sha256"] is None
    finally:
        wl.close()


def test_dict_keys_recursively_sorted(system_dir: Path) -> None:
    payload = build_index(None, system_dir)
    for project in payload["projects"]:
        keys = list(project.keys())
        assert keys == sorted(keys)

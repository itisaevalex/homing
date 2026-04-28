"""Tests for the Typer CLI in ``homing.cli``.

Uses ``CliRunner`` with mix_stderr=False so we can assert on stderr-style
errors without them polluting stdout assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from homing.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Basic command surface
# ---------------------------------------------------------------------------


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "homing" in result.stdout


def test_help_lists_phase_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["enumerate", "summary", "rules", "index", "query"]:
        assert cmd in result.stdout


# ---------------------------------------------------------------------------
# enumerate
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_home(tmp_path: Path) -> Path:
    """Build a tiny but realistic $HOME-like layout for end-to-end CLI runs."""
    # A project: .git + pyproject.toml
    (tmp_path / "proj-a" / ".git").mkdir(parents=True)
    (tmp_path / "proj-a" / "pyproject.toml").write_text("[project]\nname='a'\n")
    # A place: Documents
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "n.md").write_text("hi\n")
    # A place: .config
    (tmp_path / ".config").mkdir()
    return tmp_path


def test_enumerate_writes_enumeration_json(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    result = runner.invoke(
        app,
        ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)],
    )
    assert result.exit_code == 0, result.stdout
    enumeration = sysdir / "enumeration.json"
    assert enumeration.is_file()
    data = json.loads(enumeration.read_text())
    assert "projects" in data
    assert "places" in data


def test_enumerate_creates_worklist_with_units(
    fixture_home: Path, tmp_path: Path
) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app,
        ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)],
    )
    assert (sysdir / "worklist.sqlite").is_file()


def test_enumerate_is_idempotent_on_reruns(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    first = runner.invoke(
        app,
        ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)],
    )
    second = runner.invoke(
        app,
        ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)],
    )
    assert first.exit_code == 0
    assert second.exit_code == 0


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


def test_rules_errors_when_no_worklist(tmp_path: Path) -> None:
    sysdir = tmp_path / "empty"
    result = runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    assert result.exit_code == 2
    assert "worklist" in result.stdout.lower() or "worklist" in (result.stderr or "").lower()


def test_rules_runs_after_enumerate(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    enum_result = runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    assert enum_result.exit_code == 0
    rules_result = runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    assert rules_result.exit_code == 0, rules_result.stdout
    assert "evaluated" in rules_result.stdout.lower()


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def test_index_writes_index_json(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    result = runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    assert result.exit_code == 0, result.stdout
    idx_path = sysdir / "index.json"
    assert idx_path.is_file()
    data = json.loads(idx_path.read_text())
    assert "projects" in data
    assert "places" in data
    assert "schema_version" in data


def test_index_is_idempotent_modulo_timestamp(
    fixture_home: Path, tmp_path: Path
) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    first = json.loads((sysdir / "index.json").read_text())
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    second = json.loads((sysdir / "index.json").read_text())
    first.pop("generated_at")
    second.pop("generated_at")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


def test_query_list_errors_without_index(tmp_path: Path) -> None:
    result = runner.invoke(app, ["query", "list", "--system-dir", str(tmp_path / "x")])
    assert result.exit_code == 2


def test_query_list_runs_with_index(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    result = runner.invoke(app, ["query", "list", "--system-dir", str(sysdir)])
    assert result.exit_code == 0
    assert "unit" in result.stdout.lower()


def test_query_show_unknown_unit_errors(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    result = runner.invoke(
        app, ["query", "show", "no-such-unit", "--system-dir", str(sysdir)]
    )
    assert result.exit_code == 2


def test_query_active_runs(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    result = runner.invoke(app, ["query", "active", "--system-dir", str(sysdir)])
    assert result.exit_code == 0


def test_query_stale_runs(fixture_home: Path, tmp_path: Path) -> None:
    sysdir = tmp_path / "sys"
    runner.invoke(
        app, ["enumerate", "--system-dir", str(sysdir), "--home", str(fixture_home)]
    )
    runner.invoke(app, ["rules", "--system-dir", str(sysdir)])
    runner.invoke(app, ["index", "--system-dir", str(sysdir)])
    result = runner.invoke(app, ["query", "stale", "--system-dir", str(sysdir)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# stub commands
# ---------------------------------------------------------------------------


def test_classify_stub_returns_nonzero() -> None:
    result = runner.invoke(app, ["classify"])
    assert result.exit_code == 1


def test_draft_returns_nonzero_when_unit_missing(tmp_path: Path) -> None:
    # draft is now a real command (Phase E); without a worklist or matching unit
    # it should exit non-zero (missing prerequisite). Stays out of the LLM path
    # because the lookup fails first.
    result = runner.invoke(
        app, ["draft", "nonexistent", "--system-dir", str(tmp_path)]
    )
    assert result.exit_code != 0

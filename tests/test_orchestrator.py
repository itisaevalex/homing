"""Tests for ``homing.orchestrator`` — the rules pass over the worklist."""

from __future__ import annotations

from pathlib import Path

import pytest

from homing.orchestrator import RulesReport, run_rules
from homing.worklist import Worklist


@pytest.fixture
def fixture_tree(tmp_path: Path) -> Path:
    """Build three units on disk: one with .git, one Python, one bare dir.

    Layout::

        tmp/
        ├── alpha/                    git + python (two rules fire)
        │   ├── .git/
        │   └── pyproject.toml
        ├── beta/                     node-only (one rule fires)
        │   └── package.json
        └── gamma/                    bare dir (no rule fires)
            └── notes.txt
    """
    alpha = tmp_path / "alpha"
    (alpha / ".git").mkdir(parents=True)
    (alpha / "pyproject.toml").write_text("[project]\nname = 'alpha'\n")

    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / "package.json").write_text("{}")

    gamma = tmp_path / "gamma"
    gamma.mkdir()
    (gamma / "notes.txt").write_text("hi\n")

    return tmp_path


@pytest.fixture
def populated_worklist(fixture_tree: Path) -> Worklist:
    wl = Worklist(":memory:")
    wl.add_unit(
        "project",
        "alpha",
        str(fixture_tree / "alpha"),
        payload={"signals_found": [".git", "pyproject.toml"]},
    )
    wl.add_unit(
        "project",
        "beta",
        str(fixture_tree / "beta"),
        payload={"signals_found": ["package.json"]},
    )
    wl.add_unit(
        "project",
        "gamma",
        str(fixture_tree / "gamma"),
        payload={"signals_found": []},
    )
    yield wl
    wl.close()


def test_run_rules_returns_report(populated_worklist: Worklist) -> None:
    report = run_rules(populated_worklist)
    assert isinstance(report, RulesReport)
    assert report.total_units == 3


def test_alpha_gets_git_and_python_findings(populated_worklist: Worklist) -> None:
    run_rules(populated_worklist)
    findings = populated_worklist.findings_for("alpha")
    rules_fired = {f["rule"] for f in findings}
    assert "is-git-project" in rules_fired
    assert "is-python-project" in rules_fired


def test_beta_gets_node_finding(populated_worklist: Worklist) -> None:
    run_rules(populated_worklist)
    findings = populated_worklist.findings_for("beta")
    rules_fired = {f["rule"] for f in findings}
    assert "is-node-project" in rules_fired


def test_gamma_gets_no_findings(populated_worklist: Worklist) -> None:
    run_rules(populated_worklist)
    findings = populated_worklist.findings_for("gamma")
    assert findings == []


def test_units_with_findings_advance_to_rules_evaluated(
    populated_worklist: Worklist,
) -> None:
    run_rules(populated_worklist)
    assert populated_worklist.unit("alpha")["status"] == "rules-evaluated"
    assert populated_worklist.unit("beta")["status"] == "rules-evaluated"


def test_units_without_findings_stay_discovered(populated_worklist: Worklist) -> None:
    run_rules(populated_worklist)
    assert populated_worklist.unit("gamma")["status"] == "discovered"


def test_report_counts_match_outcomes(populated_worklist: Worklist) -> None:
    report = run_rules(populated_worklist)
    assert report.units_evaluated == 2
    assert report.units_needing_llm == 1
    # alpha: 2 rules, beta: 1 rule, gamma: 0 rules => 3 findings total
    assert report.total_findings == 3


def test_by_rule_counts_sorted_and_correct(populated_worklist: Worklist) -> None:
    report = run_rules(populated_worklist)
    assert report.by_rule_counts == {
        "is-git-project": 1,
        "is-node-project": 1,
        "is-python-project": 1,
    }
    # dict iteration order should be sorted by key
    assert list(report.by_rule_counts.keys()) == sorted(report.by_rule_counts.keys())


def test_idempotent_second_run_only_processes_remaining(
    populated_worklist: Worklist,
) -> None:
    """Re-running rules should only see units still in 'discovered'."""
    first = run_rules(populated_worklist)
    second = run_rules(populated_worklist)

    # Second pass: alpha and beta are 'rules-evaluated' so they're skipped.
    # Only gamma remains.
    assert second.total_units == 1
    # No new findings the second time around (gamma still has no signals).
    assert second.total_findings == 0
    # First pass numbers are stable.
    assert first.total_findings == 3


def test_run_with_no_discovered_units(tmp_path: Path) -> None:
    wl = Worklist(":memory:")
    try:
        report = run_rules(wl)
        assert report.total_units == 0
        assert report.total_findings == 0
        assert report.units_evaluated == 0
        assert report.units_needing_llm == 0
    finally:
        wl.close()


def test_missing_path_records_event_does_not_crash(tmp_path: Path) -> None:
    wl = Worklist(":memory:")
    try:
        wl.add_unit("project", "ghost", str(tmp_path / "does-not-exist"))
        report = run_rules(wl)
        # No findings, but no crash either.
        assert report.total_units == 1
        assert report.units_needing_llm == 1
        events = wl.events_for("ghost")
        assert any("missing" in e["message"].lower() for e in events)
    finally:
        wl.close()

"""Tests for the rule plugin registry and the first three deterministic rules."""

from __future__ import annotations

from pathlib import Path

from homing.rules import all_rules
from homing.rules.base import Rule, RuleFinding, UnitSummary
from homing.rules.is_git_project import IsGitProjectRule
from homing.rules.is_node_project import IsNodeProjectRule
from homing.rules.is_python_project import IsPythonProjectRule


def _project(
    *,
    signals: list[str] | None = None,
    files: list[str] | None = None,
) -> UnitSummary:
    return UnitSummary(
        path=Path("/home/u/proj"),
        kind="project",
        signals_found=signals or [],
        size_bytes=4096,
        last_mtime=0.0,
        file_listing_top=files or [],
    )


def _place(*, name: str = "Documents") -> UnitSummary:
    return UnitSummary(
        path=Path(f"/home/u/{name}"),
        kind="place",
        signals_found=[],
        size_bytes=4096,
        last_mtime=0.0,
        file_listing_top=[],
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_returns_at_least_three_rules() -> None:
    rules = all_rules()
    names = {r.name for r in rules}
    assert "is-git-project" in names
    assert "is-node-project" in names
    assert "is-python-project" in names


def test_registry_only_returns_rule_subclasses() -> None:
    for cls in all_rules():
        assert issubclass(cls, Rule)
        assert cls is not Rule


def test_registry_skips_base_module() -> None:
    # If 'base' or '__init__' had been imported as plugins, the Rule base
    # class itself might appear in the registry.
    assert Rule not in all_rules()


# ---------------------------------------------------------------------------
# IsGitProjectRule
# ---------------------------------------------------------------------------


def test_git_rule_fires_on_git_signal() -> None:
    rule = IsGitProjectRule()
    unit = _project(signals=[".git", "package.json"])
    assert rule.applies(unit)
    finding = rule.evaluate(unit)
    assert isinstance(finding, RuleFinding)
    assert finding.rule_name == "is-git-project"
    assert finding.confidence == 1.0
    assert finding.classifications["is_git_project"] is True
    assert any(".git" in p for p, _ in finding.evidence)


def test_git_rule_skips_when_no_git() -> None:
    rule = IsGitProjectRule()
    unit = _project(signals=["package.json"])
    assert not rule.applies(unit)
    assert rule.evaluate(unit) is None


def test_git_rule_skips_places() -> None:
    rule = IsGitProjectRule()
    unit = _place()
    assert not rule.applies(unit)
    assert rule.evaluate(unit) is None


# ---------------------------------------------------------------------------
# IsNodeProjectRule
# ---------------------------------------------------------------------------


def test_node_rule_fires_on_package_json_signal() -> None:
    rule = IsNodeProjectRule()
    unit = _project(signals=["package.json"])
    finding = rule.evaluate(unit)
    assert finding is not None
    assert finding.classifications == {"stack": ["node"]}
    assert finding.evidence and "package.json" in finding.evidence[0][0]


def test_node_rule_fires_on_package_json_in_listing() -> None:
    rule = IsNodeProjectRule()
    unit = _project(signals=[], files=["package.json", "src"])
    assert rule.applies(unit)
    finding = rule.evaluate(unit)
    assert finding is not None and finding.confidence == 1.0


def test_node_rule_skips_without_package_json() -> None:
    rule = IsNodeProjectRule()
    unit = _project(signals=[".git"])
    assert rule.evaluate(unit) is None


# ---------------------------------------------------------------------------
# IsPythonProjectRule
# ---------------------------------------------------------------------------


def test_python_rule_fires_on_pyproject() -> None:
    rule = IsPythonProjectRule()
    unit = _project(signals=["pyproject.toml"])
    finding = rule.evaluate(unit)
    assert finding is not None
    assert finding.classifications == {"stack": ["python"]}


def test_python_rule_fires_on_setup_py_in_listing() -> None:
    rule = IsPythonProjectRule()
    unit = _project(signals=[], files=["setup.py"])
    finding = rule.evaluate(unit)
    assert finding is not None


def test_python_rule_fires_on_requirements_txt() -> None:
    rule = IsPythonProjectRule()
    unit = _project(signals=[], files=["requirements.txt"])
    finding = rule.evaluate(unit)
    assert finding is not None
    assert any("requirements.txt" in p for p, _ in finding.evidence)


def test_python_rule_lists_every_marker_found() -> None:
    rule = IsPythonProjectRule()
    unit = _project(
        signals=["pyproject.toml"],
        files=["pyproject.toml", "setup.py", "requirements.txt"],
    )
    finding = rule.evaluate(unit)
    assert finding is not None
    cited = {p for p, _ in finding.evidence}
    assert any("pyproject.toml" in p for p in cited)
    assert any("setup.py" in p for p in cited)
    assert any("requirements.txt" in p for p in cited)


def test_python_rule_skips_without_marker() -> None:
    rule = IsPythonProjectRule()
    unit = _project(signals=[".git", "package.json"])
    assert rule.evaluate(unit) is None

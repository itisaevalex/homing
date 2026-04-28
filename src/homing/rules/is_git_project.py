"""Rule: presence of ``.git`` ⇒ this is a git-tracked project.

Confidence is 1.0; the evidence cites the ``.git`` directory itself.
"""

from __future__ import annotations

from .base import Rule, RuleFinding, UnitSummary

_RULE_NAME = "is-git-project"


class IsGitProjectRule(Rule):
    """Match any unit whose root contains a ``.git`` entry."""

    name = _RULE_NAME

    def applies(self, unit: UnitSummary) -> bool:
        return unit.kind == "project" and (
            ".git" in unit.signals_found or ".git" in unit.file_listing_top
        )

    def evaluate(self, unit: UnitSummary) -> RuleFinding | None:
        if not self.applies(unit):
            return None
        git_path = str(unit.path / ".git")
        return RuleFinding(
            rule_name=_RULE_NAME,
            confidence=1.0,
            classifications={"is_git_project": True},
            evidence=[(git_path, ".git directory present at project root")],
        )

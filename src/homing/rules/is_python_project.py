"""Rule: ``pyproject.toml`` / ``setup.py`` / ``requirements.txt`` ⇒ Python stack.

Whichever marker(s) are found get cited individually so the manifest
author can point at the strongest evidence later.
"""

from __future__ import annotations

from .base import Rule, RuleFinding, UnitSummary

_RULE_NAME = "is-python-project"
_MARKERS = ("pyproject.toml", "setup.py", "requirements.txt")


class IsPythonProjectRule(Rule):
    """Match any project whose root contains a Python build/install marker."""

    name = _RULE_NAME

    def applies(self, unit: UnitSummary) -> bool:
        if unit.kind != "project":
            return False
        names = set(unit.signals_found) | set(unit.file_listing_top)
        return any(m in names for m in _MARKERS)

    def evaluate(self, unit: UnitSummary) -> RuleFinding | None:
        if not self.applies(unit):
            return None
        names = set(unit.signals_found) | set(unit.file_listing_top)
        hits = [m for m in _MARKERS if m in names]
        evidence = [(str(unit.path / m), f"{m} present at project root") for m in hits]
        return RuleFinding(
            rule_name=_RULE_NAME,
            confidence=1.0,
            classifications={"stack": ["python"]},
            evidence=evidence,
        )

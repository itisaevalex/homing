"""Rule: ``package.json`` ⇒ Node/JS stack.

Confidence is 1.0; the evidence cites ``package.json``. The rule adds
``"node"`` to ``classifications["stack"]`` so multiple stack rules can
combine non-destructively.
"""

from __future__ import annotations

from .base import Rule, RuleFinding, UnitSummary

_RULE_NAME = "is-node-project"
_MARKER = "package.json"


class IsNodeProjectRule(Rule):
    """Match any project whose root contains ``package.json``."""

    name = _RULE_NAME

    def applies(self, unit: UnitSummary) -> bool:
        if unit.kind != "project":
            return False
        return _MARKER in unit.signals_found or _MARKER in unit.file_listing_top

    def evaluate(self, unit: UnitSummary) -> RuleFinding | None:
        if not self.applies(unit):
            return None
        marker_path = str(unit.path / _MARKER)
        return RuleFinding(
            rule_name=_RULE_NAME,
            confidence=1.0,
            classifications={"stack": ["node"]},
            evidence=[(marker_path, "package.json present at project root")],
        )

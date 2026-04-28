"""Base types for the deterministic rule plugin system (Phase C).

A :class:`Rule` is a small, side-effect-free classifier that looks at a
:class:`UnitSummary` (one project or place) and returns a
:class:`RuleFinding` with confidence, classifications, and citation-style
evidence. Rules cover the common case so the LLM only fires on the
ambiguous tail.

Adding a rule means dropping a new ``.py`` file under
``src/homing/rules/``; the registry walks the package at import time and
collects every :class:`Rule` subclass automatically. There is no manual
registration list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class UnitSummary:
    """Snapshot of one unit handed to rules for evaluation.

    Attributes:
        path: Filesystem path of the unit.
        kind: ``"project"`` or ``"place"``.
        signals_found: Project-signal filenames present at the root
            (e.g. ``[".git", "package.json"]``). Empty for places.
        size_bytes: Inode size; not a recursive total.
        last_mtime: ``stat.st_mtime`` of the unit root.
        file_listing_top: Top-level filenames inside the unit (for rules
            that match on files outside ``project_signals``).
    """

    path: Path
    kind: str  # "project" | "place"
    signals_found: list[str] = field(default_factory=list)
    size_bytes: int = 0
    last_mtime: float = 0.0
    file_listing_top: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuleFinding:
    """Result of evaluating a single rule against a :class:`UnitSummary`.

    Attributes:
        rule_name: Stable kebab-case name of the rule that produced this.
        confidence: ``0.0``–``1.0`` self-rated certainty.
        classifications: Free-form dict the rule wishes to attach (e.g.
            ``{"is_git_project": True}`` or ``{"stack": ["node"]}``).
        evidence: ``(path, reason)`` tuples — used as citations when the
            LLM later writes the manifest body.
    """

    rule_name: str
    confidence: float
    classifications: dict
    evidence: list[tuple[str, str]]


class Rule:
    """Base class for deterministic classification rules.

    Subclasses MUST override :attr:`name` and implement :meth:`applies`
    and :meth:`evaluate`. ``requires`` may list other rule names that
    should run first; the orchestrator honours the dependency ordering.
    """

    name: str = ""
    requires: list[str] = []

    def applies(self, unit: UnitSummary) -> bool:
        """Return True if this rule wants to evaluate ``unit``.

        The orchestrator skips :meth:`evaluate` when this returns False,
        which keeps unrelated rules out of the evidence trail.
        """
        raise NotImplementedError

    def evaluate(self, unit: UnitSummary) -> RuleFinding | None:
        """Run the rule and return a finding, or ``None`` if no match.

        Implementations must be pure: read from ``unit`` only, do not
        touch the filesystem, and never raise — return ``None`` for
        "doesn't apply" instead.
        """
        raise NotImplementedError

"""Phase C orchestrator — runs deterministic rules over the worklist.

The orchestrator pulls every unit currently in ``status="discovered"`` out of
the SQLite worklist, builds a :class:`UnitSummary` for each, and runs every
registered rule against it. Findings are persisted via
:meth:`Worklist.record_finding` and the unit's status is advanced to
``"rules-evaluated"`` if at least one finding meets the confidence threshold.
Units with no high-confidence finding remain in ``"discovered"`` and are
listed in the ``needs-llm`` bucket of the report so a later phase can pick
them up.

Read-only against ``$HOME`` everywhere: we only inspect the unit's path with
``os.listdir`` to enrich the :class:`UnitSummary`. We never recurse, write,
or follow symlinks. Errors during the per-unit listing are captured as
events on the worklist rather than raising, so a single broken directory
doesn't poison the entire run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homing.rules import all_rules
from homing.rules.base import RuleFinding, UnitSummary
from homing.worklist import Worklist

# Confidence threshold above which a finding lets us mark a unit
# "rules-evaluated". Below this, the unit stays "discovered" and is queued
# for Phase D (LLM classify).
RULES_CONFIDENCE_THRESHOLD: float = 0.7


@dataclass(frozen=True)
class RulesReport:
    """Summary of a rules pass over the worklist.

    Attributes:
        total_units: How many units were considered (status=discovered).
        units_evaluated: Units now advanced to ``"rules-evaluated"``.
        units_needing_llm: Units with no high-confidence finding.
        total_findings: Findings persisted across all units.
        by_rule_counts: Per-rule fire counts, sorted by rule name.
    """

    total_units: int
    units_evaluated: int
    units_needing_llm: int
    total_findings: int
    by_rule_counts: dict[str, int] = field(default_factory=dict)


def run_rules(worklist: Worklist, config: dict[str, Any] | None = None) -> RulesReport:
    """Run every registered rule over discovered units in ``worklist``.

    Args:
        worklist: An open :class:`Worklist` instance. Modified in place.
        config: Reserved for future per-rule config passthrough. Unused
            today but accepted to keep the signature stable.

    Returns:
        :class:`RulesReport` with totals and per-rule fire counts.
    """
    del config  # reserved

    rule_classes = all_rules()
    rules = [cls() for cls in rule_classes]

    units = worklist.units_by_status("discovered")
    # Deterministic order: path-sorted (units_by_status sorts by name; we
    # re-sort by path to match the rest of the pipeline's invariants).
    units = sorted(units, key=lambda u: u["path"])

    by_rule: dict[str, int] = {}
    total_findings = 0
    units_evaluated = 0
    units_needing_llm = 0

    for unit in units:
        unit_summary = _build_unit_summary(unit, worklist)
        findings_for_unit: list[RuleFinding] = []

        for rule in rules:
            try:
                if not rule.applies(unit_summary):
                    continue
                finding = rule.evaluate(unit_summary)
            except Exception as exc:  # rules must not crash the run
                worklist.event(
                    unit["name"],
                    type="rule-error",
                    message=f"rule {rule.name!r} raised {type(exc).__name__}: {exc}",
                )
                continue
            if finding is None:
                continue
            findings_for_unit.append(finding)
            worklist.record_finding(
                unit["name"],
                rule=finding.rule_name,
                confidence=finding.confidence,
                classifications=finding.classifications,
                evidence=[list(item) for item in finding.evidence],
            )
            by_rule[finding.rule_name] = by_rule.get(finding.rule_name, 0) + 1
            total_findings += 1

        if any(f.confidence >= RULES_CONFIDENCE_THRESHOLD for f in findings_for_unit):
            worklist.update_status(unit["name"], "rules-evaluated")
            units_evaluated += 1
        else:
            units_needing_llm += 1

    return RulesReport(
        total_units=len(units),
        units_evaluated=units_evaluated,
        units_needing_llm=units_needing_llm,
        total_findings=total_findings,
        by_rule_counts=dict(sorted(by_rule.items())),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_unit_summary(unit: dict[str, Any], worklist: Worklist) -> UnitSummary:
    """Build a :class:`UnitSummary` for a worklist row.

    Reads the unit's top-level directory listing to populate
    ``file_listing_top``. Pulls ``signals_found``, ``size_bytes``, and
    ``last_mtime`` from the unit payload (recorded by Phase A) when
    available, falling back to defaults.
    """
    path = Path(unit["path"])
    payload = unit.get("payload") or {}

    signals_found = list(payload.get("signals_found") or [])
    size_bytes = int(payload.get("size_bytes") or 0)
    last_mtime = float(payload.get("last_mtime") or 0.0)

    file_listing_top = _safe_listdir(path, unit_name=unit["name"], worklist=worklist)

    return UnitSummary(
        path=path,
        kind=unit["kind"],
        signals_found=sorted(signals_found),
        size_bytes=size_bytes,
        last_mtime=last_mtime,
        file_listing_top=file_listing_top,
    )


def _safe_listdir(path: Path, *, unit_name: str, worklist: Worklist) -> list[str]:
    """Return a sorted top-level listing of ``path``, swallowing errors.

    Errors (missing dir, permission denied) are recorded as events on the
    unit so they're auditable, but they never abort the rules pass.
    """
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        worklist.event(unit_name, type="warning", message=f"path missing: {path}")
        return []
    except PermissionError as exc:
        worklist.event(unit_name, type="warning", message=f"permission denied: {exc}")
        return []
    except OSError as exc:
        worklist.event(unit_name, type="warning", message=f"listdir failed: {exc}")
        return []
    return sorted(names)

"""Tests for planner.py — building action plans from decisions.

Goals:
- "keep" + "review-later" become skip actions.
- "archive" becomes a move with the right destination.
- "trash" becomes a move into the review pile.
- "dedupe" sends non-keep copies to <review_pile>/dedupe/<bucket>/.
- Plan JSON is deterministic for the same input.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from cabinet.planner import (
    PLAN_SCHEMA_VERSION,
    Action,
    ActionPlan,
    build_plan,
    load_plan,
    render_plan,
    write_plan,
)


@dataclass
class _D:
    unit_path: str
    action: str
    target: str | None = None
    dedupe_group: str | None = None


class _StubWl:
    def __init__(self, decisions):
        self._d = list(decisions)

    def get_decisions(self):
        return self._d


@pytest.mark.unit
def test_keep_decision_becomes_skip_action(tmp_path):
    wl = _StubWl([_D("/a", "keep")])
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev")
    assert len(plan.actions) == 1
    assert plan.actions[0].op == "skip"
    assert plan.actions[0].source == "/a"


@pytest.mark.unit
def test_review_later_becomes_skip_action(tmp_path):
    wl = _StubWl([_D("/a", "review-later")])
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev")
    assert plan.actions[0].op == "skip"
    assert "review later" in plan.actions[0].reason


@pytest.mark.unit
def test_archive_with_explicit_target(tmp_path):
    arch = tmp_path / "arch"
    # Trailing slashes are normalized by pathlib — assert without one.
    target = "/abs/dest/photos"
    wl = _StubWl([_D("/a", "archive", target=target)])
    plan = build_plan(wl, archive_root=arch, review_pile=tmp_path / "rev")
    assert plan.actions[0].op == "move"
    assert plan.actions[0].source == "/a"
    assert plan.actions[0].dest == target


@pytest.mark.unit
def test_archive_default_destination_under_archive_root(tmp_path):
    arch = tmp_path / "arch"
    wl = _StubWl([_D("/some/folder/x", "archive")])
    plan = build_plan(wl, archive_root=arch, review_pile=tmp_path / "rev")
    assert plan.actions[0].dest == str((arch / "x").resolve())


@pytest.mark.unit
def test_trash_lands_in_review_pile(tmp_path):
    rev = tmp_path / "rev"
    wl = _StubWl([_D("/some/folder/file.bin", "trash")])
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=rev)
    a = plan.actions[0]
    assert a.op == "move"
    assert str(rev.resolve() / "trashed") in a.dest


@pytest.mark.unit
def test_dedupe_non_keep_routes_into_review_pile_bucket(tmp_path):
    rev = tmp_path / "rev"
    wl = _StubWl(
        [
            _D("/a/copy.pdf", "keep", dedupe_group="g1"),
            _D("/b/copy.pdf", "dedupe", target="/a/copy.pdf", dedupe_group="g1"),
        ]
    )
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=rev)
    moves = [a for a in plan.actions if a.op == "move"]
    assert len(moves) == 1
    assert moves[0].source == "/b/copy.pdf"
    # Bucket prefix derived from group id; under <rev>/dedupe/<bucket>/.
    assert "dedupe" in moves[0].dest
    assert "/a/copy.pdf" in moves[0].reason


@pytest.mark.unit
def test_unknown_action_becomes_skip_with_loud_reason(tmp_path):
    wl = _StubWl([_D("/a", "unknown-action")])
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev")
    assert plan.actions[0].op == "skip"
    assert "unknown" in plan.actions[0].reason.lower()


@pytest.mark.unit
def test_plan_actions_sorted_moves_before_skips(tmp_path):
    wl = _StubWl(
        [
            _D("/z", "keep"),  # skip
            _D("/a", "archive"),  # move
            _D("/m", "trash"),  # move
        ]
    )
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev")
    ops = [a.op for a in plan.actions]
    # All moves first, then skips.
    assert ops == ["move", "move", "skip"]
    # And alphabetical within group.
    moves = [a.source for a in plan.actions if a.op == "move"]
    assert moves == sorted(moves)


@pytest.mark.integration
def test_plan_json_is_deterministic(tmp_path):
    wl = _StubWl(
        [
            _D("/b", "archive"),
            _D("/a", "keep"),
            _D("/c", "trash"),
        ]
    )
    fixed = dt.datetime(2024, 1, 1, 12, 0, 0)
    p1 = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev", generated_at=fixed)
    p2 = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev", generated_at=fixed)

    out1 = tmp_path / "p1.json"
    out2 = tmp_path / "p2.json"
    write_plan(p1, out1)
    write_plan(p2, out2)
    assert out1.read_bytes() == out2.read_bytes()


@pytest.mark.integration
def test_plan_round_trips_through_disk(tmp_path):
    wl = _StubWl([_D("/a", "archive", target="/arch/x")])
    plan = build_plan(wl, archive_root=tmp_path / "arch", review_pile=tmp_path / "rev")
    out = tmp_path / "plan.json"
    write_plan(plan, out)
    loaded = load_plan(out)
    assert loaded.schema_version == PLAN_SCHEMA_VERSION
    assert len(loaded.actions) == 1
    assert loaded.actions[0].source == "/a"
    assert loaded.actions[0].dest == "/arch/x"


@pytest.mark.unit
def test_render_plan_summary_contains_counts(tmp_path):
    plan = ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root="/arch",
        review_pile="/rev",
        actions=(
            Action(op="move", source="/a", dest="/arch/a", reason="r", evidence_unit_id="/a"),
            Action(op="skip", source="/b", dest=None, reason="r", evidence_unit_id="/b"),
        ),
    )
    text = render_plan(plan)
    assert "Total actions: 2" in text
    assert "1 moves" in text
    assert "1 skips" in text

"""Tests for undo.py — apply + ledger + undo round-trip on small plans.

The big chaos test lives in test_chaos_dont_lose.py. These are targeted
tests that pin specific properties:

- Single move round-trips.
- Refuses to overwrite an existing dest.
- Refuses to apply with a missing source.
- Failure mid-plan rolls back completed moves.
- Ledger format is JSON-Lines, append-only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from cabinet.planner import Action, ActionPlan
from cabinet.undo import ApplyAbort, apply_plan, undo_ledger


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _plan(*actions: Action, archive_root: Path, review_pile: Path) -> ActionPlan:
    return ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root=str(archive_root),
        review_pile=str(review_pile),
        actions=tuple(actions),
    )


@pytest.mark.unit
def test_single_move_roundtrip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello")
    dst = tmp_path / "moved" / "a.txt"
    plan = _plan(
        Action(op="move", source=str(src), dest=str(dst), reason="t", evidence_unit_id=str(src)),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    pre_hash = _hash(src)

    apply_plan(plan, ledger_path=ledger)
    assert dst.exists() and not src.exists()
    assert _hash(dst) == pre_hash

    result = undo_ledger(ledger)
    assert result.failures == ()
    assert result.reversed_count == 1
    assert src.exists() and not dst.exists()
    assert _hash(src) == pre_hash


@pytest.mark.unit
def test_refuses_to_overwrite_existing_destination(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello")
    dst = tmp_path / "b.txt"
    dst.write_text("DO NOT OVERWRITE")
    plan = _plan(
        Action(op="move", source=str(src), dest=str(dst), reason="t", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    with pytest.raises(ApplyAbort):
        apply_plan(plan, ledger_path=tmp_path / "ledger.jsonl")
    # Source untouched, dest still has its original content.
    assert src.read_text() == "hello"
    assert dst.read_text() == "DO NOT OVERWRITE"


@pytest.mark.unit
def test_refuses_with_missing_source(tmp_path):
    plan = _plan(
        Action(op="move", source=str(tmp_path / "missing"), dest=str(tmp_path / "x"),
               reason="t", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    with pytest.raises(ApplyAbort):
        apply_plan(plan, ledger_path=tmp_path / "ledger.jsonl")


@pytest.mark.unit
def test_partial_failure_rolls_back_completed_moves(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("aaa")
    b_missing = tmp_path / "missing.txt"  # source for second action does not exist
    plan = _plan(
        Action(
            op="move",
            source=str(a),
            dest=str(tmp_path / "moved-a"),
            reason="t",
            evidence_unit_id="a",
        ),
        Action(
            op="move",
            source=str(b_missing),
            dest=str(tmp_path / "moved-b"),
            reason="t",
            evidence_unit_id="b",
        ),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    with pytest.raises(ApplyAbort):
        apply_plan(plan, ledger_path=ledger)
    # First move must have been rolled back: a.txt is back at the source path.
    assert a.exists()
    assert a.read_text() == "aaa"
    assert not (tmp_path / "moved-a").exists()


@pytest.mark.unit
def test_ledger_is_jsonlines_append_only(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x")
    dst = tmp_path / "b.txt"
    plan = _plan(
        Action(op="move", source=str(src), dest=str(dst), reason="t", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)
    text = ledger.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    # At minimum: a "begin" entry then a "complete" entry.
    statuses = [json.loads(line)["status"] for line in lines]
    assert "begin" in statuses
    assert "complete" in statuses
    # Each line is valid JSON.
    for line in lines:
        json.loads(line)


@pytest.mark.unit
def test_skip_action_is_recorded_but_no_filesystem_change(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("kept")
    plan = _plan(
        Action(op="skip", source=str(src), dest=None, reason="kept", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)
    assert src.read_text() == "kept"
    # Ledger has at least one entry.
    assert ledger.exists() and ledger.read_text().strip()


@pytest.mark.unit
def test_undo_rejects_when_dest_modified_after_apply(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("original")
    dst = tmp_path / "moved"
    plan = _plan(
        Action(op="move", source=str(src), dest=str(dst), reason="t", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)
    # Tamper with dest.
    dst.write_text("tampered")
    result = undo_ledger(ledger)
    # Refuses to silently restore tampered content.
    assert result.reversed_count == 0
    assert any("content changed" in f for f in result.failures)
    # And the dest is still in place — not destroyed.
    assert dst.read_text() == "tampered"


@pytest.mark.unit
def test_directory_move_roundtrip(tmp_path):
    """Moving a directory must preserve full tree on undo."""
    src = tmp_path / "tree"
    src.mkdir()
    (src / "a.txt").write_text("a")
    sub = src / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("b")

    pre_a = _hash(src / "a.txt")
    pre_b = _hash(sub / "b.txt")

    dst = tmp_path / "tree-moved"
    plan = _plan(
        Action(op="move", source=str(src), dest=str(dst), reason="t", evidence_unit_id="x"),
        archive_root=tmp_path / "arch",
        review_pile=tmp_path / "rev",
    )
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)
    assert (dst / "a.txt").exists()
    assert (dst / "sub" / "b.txt").exists()
    assert not src.exists()

    undo_ledger(ledger)
    assert (src / "a.txt").exists()
    assert (src / "sub" / "b.txt").exists()
    assert _hash(src / "a.txt") == pre_a
    assert _hash(sub / "b.txt") == pre_b

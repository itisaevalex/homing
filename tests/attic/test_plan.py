"""Unit tests for attic.plan — filtering, key collision-resistance, round-trip."""

from __future__ import annotations

import os
import time

import pytest

from attic.plan import build_plan_from_paths, load_plan, write_plan


@pytest.mark.unit
def test_filters_by_min_size(tmp_path):
    small = tmp_path / "small.txt"
    small.write_bytes(b"x" * 10)
    big = tmp_path / "big.txt"
    big.write_bytes(b"y" * 5000)

    plan = build_plan_from_paths([small, big], remote="r:b", min_size=1000)
    sources = [a.source for a in plan.actions]
    assert str(big.resolve()) in sources
    assert str(small.resolve()) not in sources


@pytest.mark.unit
def test_filters_by_min_age_days(tmp_path):
    now = time.time()
    fresh = tmp_path / "fresh.txt"
    fresh.write_bytes(b"z" * 100)
    old = tmp_path / "old.txt"
    old.write_bytes(b"z" * 100)
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))

    plan = build_plan_from_paths([fresh, old], remote="r:b", min_age_days=30, now=now)
    sources = [a.source for a in plan.actions]
    assert str(old.resolve()) in sources
    assert str(fresh.resolve()) not in sources


@pytest.mark.unit
def test_dir_size_is_recursive(tmp_path):
    d = tmp_path / "dir"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.bin").write_bytes(b"q" * 4000)
    plan = build_plan_from_paths([d], remote="r:b", min_size=3000)
    assert len(plan.actions) == 1
    assert plan.actions[0].is_dir is True


@pytest.mark.unit
def test_missing_path_dropped(tmp_path):
    plan = build_plan_from_paths([tmp_path / "ghost"], remote="r:b")
    assert plan.actions == ()


@pytest.mark.unit
def test_remote_key_collision_resistance(tmp_path):
    # Two files share a basename but live in different parents.
    a = tmp_path / "p1" / "logs"
    b = tmp_path / "p2" / "logs"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    plan = build_plan_from_paths([a, b], remote="r:b")
    keys = [act.remote_key for act in plan.actions]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert all(k.startswith("logs-") for k in keys)


@pytest.mark.unit
def test_json_roundtrip(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"data" * 100)
    plan = build_plan_from_paths([f], remote="attic-crypt:vault")
    out = tmp_path / "plan.json"
    write_plan(plan, out)
    reloaded = load_plan(out)
    assert reloaded.remote == plan.remote
    assert reloaded.schema_version == plan.schema_version
    assert [a.to_dict() for a in reloaded.actions] == [a.to_dict() for a in plan.actions]


@pytest.mark.unit
def test_deterministic_ordering(tmp_path):
    files = []
    for name in ("c", "a", "b"):
        p = tmp_path / f"{name}.txt"
        p.write_bytes(b"x" * 50)
        files.append(p)
    plan = build_plan_from_paths(files, remote="r:b")
    sources = [a.source for a in plan.actions]
    assert sources == sorted(sources)

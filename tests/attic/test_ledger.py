"""Unit tests for attic.ledger — JSONL append/read, manifest, hash parity."""

from __future__ import annotations

import os

import pytest

from attic import ledger as _ledger
from attic.ledger import LedgerEntry, build_manifest, latest_by_unit


def _entry(unit_id: str, status: str, **over) -> LedgerEntry:
    base = dict(
        schema_version=1,
        unit_id=unit_id,
        op="push",
        status=status,
        source_path=unit_id,
        is_dir=False,
        remote_backend="attic-crypt:vault",
        remote_key="k",
        key_fingerprint="fp",
        local_content_hash="h",
        manifest=None,
        source_mode=0o644,
        size=1,
        object_count=1,
        remote_storage_tier="STANDARD",
        remote_size=1,
        roundtrip_verified=False,
        verify_method="download-roundtrip",
        tombstone_path=None,
        staging_path=None,
        timestamp=1.0,
        rclone_version="rclone v1.74.2",
    )
    base.update(over)
    return LedgerEntry(**base)


@pytest.mark.unit
def test_append_and_read_roundtrip(tmp_path):
    path = tmp_path / "l.jsonl"
    e1 = _entry("/a", "begin")
    e2 = _entry("/a", "verified", roundtrip_verified=True)
    _ledger.append(path, e1)
    _ledger.append(path, e2)
    rows = _ledger.read(path)
    assert [r.status for r in rows] == ["begin", "verified"]
    # Each line is valid JSON.
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


@pytest.mark.unit
def test_latest_by_unit_picks_live_status(tmp_path):
    path = tmp_path / "l.jsonl"
    _ledger.append(path, _entry("/a", "begin", timestamp=1.0))
    _ledger.append(path, _entry("/a", "uploaded", timestamp=2.0))
    _ledger.append(path, _entry("/a", "verified", timestamp=3.0, roundtrip_verified=True))
    _ledger.append(path, _entry("/b", "failed", timestamp=2.5))
    latest = latest_by_unit(path)
    assert latest["/a"].status == "verified"
    assert latest["/b"].status == "failed"


@pytest.mark.unit
def test_read_missing_ledger_is_empty(tmp_path):
    assert _ledger.read(tmp_path / "absent.jsonl") == []


@pytest.mark.unit
def test_build_manifest_captures_symlink_emptydir_and_modes(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "data.txt"
    f.write_text("payload")
    f.chmod(0o600)
    (root / "emptydir").mkdir()
    os.symlink("sub/data.txt", root / "link")

    manifest = build_manifest(root)
    by_rel = {e["rel"]: e for e in manifest}

    assert by_rel["sub/data.txt"]["type"] == "file"
    assert by_rel["sub/data.txt"]["mode"] == 0o600
    assert by_rel["sub/data.txt"]["sha256"]

    assert by_rel["emptydir"]["type"] == "emptydir"
    assert by_rel["link"]["type"] == "symlink"
    assert by_rel["link"]["target"] == "sub/data.txt"


@pytest.mark.unit
def test_content_hash_matches_cabinet_dir_tree(tmp_path):
    from cabinet.undo import _hash_dir_tree, _hash_file

    root = tmp_path / "tree"
    (root / "x").mkdir(parents=True)
    (root / "x" / "f.txt").write_text("abc")
    assert _ledger.content_hash(root) == _hash_dir_tree(root)
    assert _ledger.content_hash(root).startswith("dir-tree:")

    f = tmp_path / "single.txt"
    f.write_text("single")
    assert _ledger.content_hash(f) == _hash_file(f)

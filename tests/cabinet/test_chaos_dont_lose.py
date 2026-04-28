"""THE LOAD-BEARING TEST.

If this test fails, cabinet's core promise — "nothing is lost; every action
is reversible" — is broken. Do not commit until this test passes.

The test:
1. Builds a fixture filesystem with diverse content (text, binaries, nested
   dirs, special chars in names, files with spaces, mixed permissions
   including some 600-mode files).
2. Snapshots pre-state: every file's (path, sha256, mode, mtime).
3. Builds a synthetic plan touching every category: archive ~30%, dedupe
   ~20%, trash ~20%, keep ~30%.
4. apply_plan(...).
5. Verifies post-state matches the plan.
6. undo_ledger(...).
7. Verifies post-undo state == pre-state, BYTE-IDENTICAL on every (path,
   sha256, mode). If ANY file differs, the test FAILS.

Variations:
- Plan that fails halfway → partial-undo restores everything.
- Plan with concurrent rename → aborts cleanly without destruction.
- Cross-filesystem moves are exercised via copy+verify+remove path.

Constraints: <5 seconds, stdlib + pytest only.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path

import pytest

from cabinet.planner import Action, ActionPlan
from cabinet.undo import ApplyAbort, apply_plan, undo_ledger


# ---------------------------------------------------------------------------
# Fixture filesystem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSnapshot:
    rel_path: str
    sha256: str
    mode: int
    size: int


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_tree(root: Path) -> dict[str, FileSnapshot]:
    """Walk ``root`` and snapshot every file under it. Returns rel_path → snapshot."""
    out: dict[str, FileSnapshot] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        out[rel] = FileSnapshot(
            rel_path=rel,
            sha256=_hash_file(p),
            mode=stat_mod.S_IMODE(st.st_mode),
            size=st.st_size,
        )
    return out


def _build_fixture_corpus(root: Path) -> list[Path]:
    """Create a diverse fixture filesystem and return the list of file paths."""
    files: list[Path] = []

    def _w(rel: str, content: bytes, mode: int = 0o644) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        p.chmod(mode)
        files.append(p)
        return p

    # Text files
    _w("docs/notes.txt", b"plain text notes\n")
    _w("docs/readme.md", b"# readme\n\nsome markdown\n")
    # Binary file (random-ish bytes — deterministic for repeatability).
    _w("bin/blob.bin", bytes((i * 7) % 256 for i in range(4096)))
    # Files with spaces in names.
    _w("photos/Greece 2019/IMG 0001.jpg", b"FAKE-JPEG-1")
    _w("photos/Greece 2019/IMG 0002.jpg", b"FAKE-JPEG-2")
    # Special characters (non-ASCII).
    _w("misc/привет.txt", "приветствие\n".encode("utf-8"))
    _w("misc/emoji-📷.png", b"FAKE-PNG-EMOJI")
    # Restricted permissions (600).
    _w("private/secret.key", b"SECRET-KEY-MATERIAL", mode=0o600)
    _w("private/passport.pdf", b"FAKE-PDF-PASSPORT", mode=0o600)
    # Larger nested folder (still small for speed).
    for i in range(8):
        _w(f"projects/p1/file_{i:03d}.txt", f"content {i}\n".encode())
    # A duplicate-pair fixture: same content under two different paths.
    _w("dupes/copy_a.bin", b"DUPLICATE-PAYLOAD")
    _w("dupes/copy_b.bin", b"DUPLICATE-PAYLOAD")
    # A "trash candidate".
    _w("downloads/installer.dmg", b"FAKE-DMG-CONTENTS")

    return files


def _categorize(files: list[Path]) -> dict[str, list[Path]]:
    """Pick categories that roughly match 30/20/20/30 split."""
    keep: list[Path] = []
    archive: list[Path] = []
    trash: list[Path] = []
    dedupe_keep: list[Path] = []
    dedupe_drop: list[Path] = []

    for p in files:
        name = p.as_posix()
        if "dupes/copy_a.bin" in name:
            dedupe_keep.append(p)
        elif "dupes/copy_b.bin" in name:
            dedupe_drop.append(p)
        elif "downloads/" in name or "private/secret.key" in name:
            trash.append(p)
        elif (
            "Greece 2019" in name
            or "bin/blob.bin" in name
            or "misc/" in name
            or "projects/p1/file_000" in name
            or "projects/p1/file_001" in name
            or "projects/p1/file_002" in name
        ):
            archive.append(p)
        else:
            keep.append(p)

    return {
        "keep": keep,
        "archive": archive,
        "trash": trash,
        "dedupe_keep": dedupe_keep,
        "dedupe_drop": dedupe_drop,
    }


def _build_plan(
    cats: dict[str, list[Path]],
    *,
    archive_root: Path,
    review_pile: Path,
) -> ActionPlan:
    actions: list[Action] = []
    for p in cats["keep"]:
        actions.append(
            Action(op="skip", source=str(p), dest=None, reason="keep", evidence_unit_id=str(p))
        )
    for p in cats["archive"]:
        dest = archive_root / p.name
        actions.append(
            Action(op="move", source=str(p), dest=str(dest), reason="archive", evidence_unit_id=str(p))
        )
    for p in cats["trash"]:
        dest = review_pile / "trashed" / p.name
        actions.append(
            Action(op="move", source=str(p), dest=str(dest), reason="trash", evidence_unit_id=str(p))
        )
    for p in cats["dedupe_drop"]:
        dest = review_pile / "dedupe" / "g1" / p.name
        actions.append(
            Action(op="move", source=str(p), dest=str(dest), reason="dedupe", evidence_unit_id=str(p))
        )
    for p in cats["dedupe_keep"]:
        actions.append(
            Action(op="skip", source=str(p), dest=None, reason="dedupe-keep", evidence_unit_id=str(p))
        )
    # Deterministic order to avoid collisions on shared parents.
    actions.sort(key=lambda a: (a.op, a.source))
    return ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root=str(archive_root),
        review_pile=str(review_pile),
        actions=tuple(actions),
    )


# ---------------------------------------------------------------------------
# THE LOAD-BEARING TEST
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chaos_dont_lose_anything(tmp_path):
    """Apply a diverse plan, undo, verify byte-identical pre-state."""
    src_root = tmp_path / "src"
    src_root.mkdir()
    archive_root = tmp_path / "archive"
    review_pile = tmp_path / "review-pile"

    files = _build_fixture_corpus(src_root)
    pre_snapshot = _snapshot_tree(src_root)
    assert len(pre_snapshot) >= 18, "fixture should have a diverse file count"

    cats = _categorize(files)
    # Sanity check on the 30/20/20/30 split.
    total = len(files)
    assert len(cats["archive"]) >= total * 0.20  # ~30%
    assert len(cats["trash"]) >= 1
    assert len(cats["dedupe_drop"]) >= 1

    plan = _build_plan(cats, archive_root=archive_root, review_pile=review_pile)
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)

    # Post-apply: archived files exist at archive_root; sources gone for moved.
    for p in cats["archive"]:
        assert not p.exists(), f"source still present after archive: {p}"
        assert (archive_root / p.name).exists(), f"archive dest missing: {p.name}"
    for p in cats["trash"]:
        assert not p.exists()
        assert (review_pile / "trashed" / p.name).exists()
    for p in cats["dedupe_drop"]:
        assert not p.exists()
        assert (review_pile / "dedupe" / "g1" / p.name).exists()
    # Keep + dedupe_keep should be untouched.
    for p in cats["keep"] + cats["dedupe_keep"]:
        assert p.exists(), f"kept file disappeared: {p}"

    # Undo.
    result = undo_ledger(ledger)
    assert result.failures == (), f"undo failures: {result.failures}"

    # Post-undo: byte-identical on every (path, sha256, mode).
    post_snapshot = _snapshot_tree(src_root)
    pre_paths = set(pre_snapshot)
    post_paths = set(post_snapshot)
    missing = pre_paths - post_paths
    extra = post_paths - pre_paths
    assert not missing, f"files missing after undo: {missing}"
    assert not extra, f"unexpected files after undo: {extra}"

    for rel, pre in pre_snapshot.items():
        post = post_snapshot[rel]
        assert pre.sha256 == post.sha256, f"hash drift: {rel}"
        assert pre.mode == post.mode, f"mode drift: {rel} ({oct(pre.mode)} vs {oct(post.mode)})"
        assert pre.size == post.size, f"size drift: {rel}"


# ---------------------------------------------------------------------------
# Variation 1 — partial failure rolls back fully
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chaos_partial_failure_rolls_back(tmp_path):
    """A plan that fails halfway must leave pre-state byte-identical."""
    src_root = tmp_path / "src"
    src_root.mkdir()
    a = src_root / "a.txt"
    a.write_text("aaa")
    b = src_root / "b.txt"
    b.write_text("bbb")
    c_missing = src_root / "ghost.txt"  # never created → second move fails

    pre = _snapshot_tree(src_root)

    plan = ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root=str(tmp_path / "arch"),
        review_pile=str(tmp_path / "rev"),
        actions=(
            Action(op="move", source=str(a), dest=str(tmp_path / "moved-a"),
                   reason="r", evidence_unit_id="a"),
            Action(op="move", source=str(b), dest=str(tmp_path / "moved-b"),
                   reason="r", evidence_unit_id="b"),
            Action(op="move", source=str(c_missing), dest=str(tmp_path / "moved-c"),
                   reason="r", evidence_unit_id="c"),
        ),
    )

    with pytest.raises(ApplyAbort):
        apply_plan(plan, ledger_path=tmp_path / "ledger.jsonl")

    # Both completed moves rolled back; no destination dirs leak.
    post = _snapshot_tree(src_root)
    assert set(pre) == set(post)
    for rel, p in pre.items():
        assert post[rel].sha256 == p.sha256
        assert post[rel].mode == p.mode

    # Confirm the move targets do not exist.
    assert not (tmp_path / "moved-a").exists()
    assert not (tmp_path / "moved-b").exists()


# ---------------------------------------------------------------------------
# Variation 2 — concurrent rename / dest pre-existing
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chaos_dest_pre_exists_aborts_cleanly(tmp_path):
    """If dest exists at apply time, abort cleanly with no destruction."""
    src_root = tmp_path / "src"
    src_root.mkdir()
    a = src_root / "a.txt"
    a.write_text("AAA")

    dst = tmp_path / "preexisting"
    dst.write_text("PRE-EXISTING-CONTENT")

    pre_dst_hash = hashlib.sha256(dst.read_bytes()).hexdigest()
    pre = _snapshot_tree(src_root)

    plan = ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root=str(tmp_path / "arch"),
        review_pile=str(tmp_path / "rev"),
        actions=(
            Action(op="move", source=str(a), dest=str(dst), reason="r", evidence_unit_id="a"),
        ),
    )
    with pytest.raises(ApplyAbort):
        apply_plan(plan, ledger_path=tmp_path / "ledger.jsonl")

    # Source is intact; dest content unchanged.
    assert a.exists()
    post = _snapshot_tree(src_root)
    assert post == pre
    post_dst_hash = hashlib.sha256(dst.read_bytes()).hexdigest()
    assert pre_dst_hash == post_dst_hash, "destructive write occurred on pre-existing dest!"


# ---------------------------------------------------------------------------
# Variation 3 — cross-filesystem moves use copy+verify+remove
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_chaos_cross_filesystem_move_path(tmp_path, monkeypatch):
    """Force the cross-FS code path even on the same FS by stubbing _same_filesystem."""
    from cabinet import undo as undo_mod

    src_root = tmp_path / "src"
    src_root.mkdir()
    a = src_root / "blob.bin"
    a.write_bytes(b"X" * 12345)
    sub = src_root / "tree"
    sub.mkdir()
    (sub / "inner.txt").write_text("inner")

    pre = _snapshot_tree(src_root)

    # Force "different filesystem" branch.
    monkeypatch.setattr(undo_mod, "_same_filesystem", lambda *a, **k: False)

    dst_a = tmp_path / "moved-blob.bin"
    dst_tree = tmp_path / "moved-tree"
    plan = ActionPlan(
        schema_version=1,
        generated_at="2024-01-01T00:00:00Z",
        archive_root=str(tmp_path / "arch"),
        review_pile=str(tmp_path / "rev"),
        actions=(
            Action(op="move", source=str(a), dest=str(dst_a), reason="r", evidence_unit_id="a"),
            Action(op="move", source=str(sub), dest=str(dst_tree), reason="r", evidence_unit_id="t"),
        ),
    )
    ledger = tmp_path / "ledger.jsonl"
    apply_plan(plan, ledger_path=ledger)

    assert dst_a.exists() and not a.exists()
    assert dst_tree.exists() and not sub.exists()
    assert (dst_tree / "inner.txt").read_text() == "inner"

    # Reverse — still uses the cross-FS path, which must restore byte-identical.
    result = undo_ledger(ledger)
    assert result.failures == ()

    post = _snapshot_tree(src_root)
    assert set(pre) == set(post)
    for rel, snap in pre.items():
        assert post[rel].sha256 == snap.sha256
        assert post[rel].mode == snap.mode

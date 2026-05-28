"""THE LOAD-BEARING TEST for attic.

attic's promise: a local copy is freed ONLY after the remote copy is proven
independently recoverable, and a restore reproduces the bytes EXACTLY (path,
sha256, mode, symlink targets, empty dirs). If this fails, attic can lose data.

Needs a live rclone + crypt remote (the crypt_remote fixture).
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from pathlib import Path

import pytest

from attic import cli as _cli
from attic import ledger as _ledger
from attic import plan as _plan
from attic import transport as _transport
from attic.cli import EvictionRefused, do_evict, do_push, do_restore
from attic.config import AtticConfig, crypt_key_fingerprint

from .conftest import build_corpus, rekey_crypt_remote, requires_rclone

pytestmark = [requires_rclone, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, tuple]:
    """rel → (kind, sha256-or-target, mode) for every entry under root."""
    out: dict[str, tuple] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = ("symlink", os.readlink(p), _lmode(p))
        elif p.is_file():
            out[rel] = ("file", _hash_file(p), _lmode(p))
        elif p.is_dir():
            empty = not any(p.iterdir())
            out[rel] = ("emptydir" if empty else "dir", None, _lmode(p))
    return out


def _lmode(p: Path) -> int:
    return stat_mod.S_IMODE(p.lstat().st_mode)


def _config_for(crypt_remote) -> AtticConfig:
    fp = crypt_key_fingerprint("attic-crypt", rclone_config_path=crypt_remote["config_path"])
    return AtticConfig(remote=crypt_remote["remote"], key_fingerprint=fp)


def _system_dir(tmp_path: Path) -> Path:
    from attic import platform as _plat

    sd = tmp_path / "attic-state"
    _plat.ensure_layout(sd)
    return sd


def _build_plan(corpus: dict[str, Path], remote: str):
    paths = [corpus["text"], corpus["binary"], corpus["weird"], corpus["secret"], corpus["tree"]]
    return _plan.build_plan_from_paths(paths, remote=remote)


# ---------------------------------------------------------------------------
# 1. push leaves local byte-identical; verified only after round trip.
# ---------------------------------------------------------------------------


def test_push_leaves_local_identical_and_verifies(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    pre = _snapshot(src)

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    plan = _build_plan(corpus, config.remote)
    results = do_push(
        plan,
        config=config,
        ledger_path=ledger,
        rclone_config_path=crypt_remote["config_path"],
    )

    # All units verified (file + dir), and round-trip-verified.
    assert all(r.status == "verified" for r in results), [r.status for r in results]
    assert all(r.roundtrip_verified for r in results)

    # Nothing moved or deleted locally.
    assert _snapshot(src) == pre

    # The verified status appears only after an "uploaded" precursor in ledger.
    statuses = [e.status for e in _ledger.read(ledger)]
    assert "uploaded" in statuses and "verified" in statuses
    assert statuses.index("uploaded") < statuses.index("verified")


# ---------------------------------------------------------------------------
# 2. evict → local gone, tombstone present; restore → byte-identical.
# ---------------------------------------------------------------------------


def test_evict_then_restore_is_byte_identical(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _build_plan(corpus, config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])

    # Snapshot the dir unit's pre-state before eviction.
    tree = corpus["tree"]
    pre_tree = _snapshot(tree)
    pre_text = _hash_file(corpus["text"])
    pre_secret_mode = _lmode(corpus["secret"])

    results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        rclone_config_path=crypt_remote["config_path"],
    )
    assert all(r.status == "purged" for r in results), [r.status for r in results]

    # Local copies gone; tombstones present.
    assert not corpus["text"].exists()
    assert not tree.exists()
    tombstones = list((sys_dir / "tombstones").glob("*.json"))
    assert len(tombstones) == len(plan.actions)

    # Restore each unit and verify byte-identity.
    for action in plan.actions:
        entry = _cli._find_unit_entry(sys_dir, ledger, action.source)
        assert entry is not None
        do_restore(
            entry, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"]
        )

    assert _hash_file(corpus["text"]) == pre_text
    assert _lmode(corpus["secret"]) == pre_secret_mode == 0o600
    # Dir tree fully reproduced — symlink target + empty dir + file hashes.
    post_tree = _snapshot(tree)
    assert post_tree == pre_tree
    assert (post_tree["emptydir"][0], post_tree["emptydir"][1]) == ("emptydir", None)
    assert post_tree["link_to_a"][0] == "symlink"


# ---------------------------------------------------------------------------
# 3. crash between tombstone write and purge → data fully recoverable.
# ---------------------------------------------------------------------------


def test_crash_between_tombstone_and_purge_loses_nothing(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    # Single file unit keeps the failure surface simple.
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])

    pre_text = _hash_file(corpus["text"])

    # Inject a crash exactly at the unlink (after the tombstone is durable).
    real_unlink = Path.unlink

    def boom(self, *a, **k):
        if self == corpus["text"]:
            raise OSError("simulated crash during purge")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", boom)

    results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        rclone_config_path=crypt_remote["config_path"],
    )
    # The unit's eviction failed.
    assert any(r.status == "failed" for r in results)

    # Local file still present + byte-identical (nothing lost).
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert corpus["text"].exists()
    assert _hash_file(corpus["text"]) == pre_text
    # Tombstone exists → the unit is independently recoverable too.
    assert list((sys_dir / "tombstones").glob("*.json"))


# ---------------------------------------------------------------------------
# 4. partial / unverified upload → evict REFUSES that unit.
# ---------------------------------------------------------------------------


def test_evict_refuses_unverified_unit(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"

    # Hand-craft a ledger entry that NEVER reached `verified` (only uploaded).
    entry = _ledger.LedgerEntry(
        schema_version=1,
        unit_id=str(corpus["text"].resolve()),
        op="push",
        status="uploaded",  # not verified
        source_path=str(corpus["text"].resolve()),
        is_dir=False,
        remote_backend=config.remote,
        remote_key="text-key",
        key_fingerprint=config.key_fingerprint,
        local_content_hash=_hash_file(corpus["text"]),
        manifest=None,
        source_mode=0o644,
        size=corpus["text"].stat().st_size,
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
    _ledger.append(ledger, entry)

    results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        rclone_config_path=crypt_remote["config_path"],
    )
    # Unverified unit skipped entirely — local file untouched.
    assert results == []
    assert corpus["text"].exists()


# ---------------------------------------------------------------------------
# 5. swap crypt password before restore → restore REFUSES.
# ---------------------------------------------------------------------------


def test_restore_refuses_after_key_swap(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])

    entry = _cli._find_unit_entry(sys_dir, ledger, str(corpus["text"].resolve()))
    assert entry is not None

    # Local copy NOT yet evicted — it must stay intact through the refusal.
    pre_text = _hash_file(corpus["text"])

    # Swap the crypt key. config.key_fingerprint now mismatches the live remote.
    rekey_crypt_remote(crypt_remote, "totally-different-key-9876543210")

    # Restore to a fresh target so the existing-target check doesn't mask it.
    target = tmp_path / "restored.txt"
    with pytest.raises(EvictionRefused):
        do_restore(
            entry,
            config=config,
            ledger_path=ledger,
            to=target,
            rclone_config_path=crypt_remote["config_path"],
        )

    assert _hash_file(corpus["text"]) == pre_text
    assert not target.exists()


def test_evict_refuses_after_key_swap(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])

    pre_text = _hash_file(corpus["text"])
    rekey_crypt_remote(crypt_remote, "yet-another-different-key-555")

    with pytest.raises(EvictionRefused):
        do_evict(
            ledger,
            config=config,
            system_dir=sys_dir,
            rclone_config_path=crypt_remote["config_path"],
        )
    # Local copy untouched.
    assert corpus["text"].exists()
    assert _hash_file(corpus["text"]) == pre_text


# ---------------------------------------------------------------------------
# 6. GLACIER tier → evict REFUSES without --allow-cold-evict.
# ---------------------------------------------------------------------------


def test_restore_dir_to_alternate_target(crypt_remote, tmp_path, monkeypatch):
    """Dir unit restores to a --to path with symlinks + empty dirs reproduced."""
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")
    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["tree"]], remote=config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])
    pre = _snapshot(corpus["tree"])

    entry = _cli._find_unit_entry(sys_dir, ledger, str(corpus["tree"].resolve()))
    target = tmp_path / "restored-tree"
    do_restore(
        entry,
        config=config,
        ledger_path=ledger,
        to=target,
        rclone_config_path=crypt_remote["config_path"],
    )
    assert _snapshot(target) == pre


def test_restore_refuses_existing_target(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")
    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])
    entry = _cli._find_unit_entry(sys_dir, ledger, str(corpus["text"].resolve()))
    # Original still present → restore must refuse to overwrite it.
    with pytest.raises(EvictionRefused):
        do_restore(
            entry, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"]
        )


def test_push_refuses_existing_remote_key(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")
    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)
    r1 = do_push(
        plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"]
    )
    assert r1[0].status == "verified"
    # Second push of the same plan → remote key exists → refuse (failed).
    r2 = do_push(
        plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"]
    )
    assert r2[0].status == "failed"
    # With force it overwrites + re-verifies.
    r3 = do_push(
        plan,
        config=config,
        ledger_path=ledger,
        force=True,
        rclone_config_path=crypt_remote["config_path"],
    )
    assert r3[0].status == "verified"


def test_evict_refuses_glacier_without_flag(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    plan = _plan.build_plan_from_paths([corpus["text"]], remote=config.remote)

    # Force the pushed object to look GLACIER-tiered.
    real_stat = _transport.object_stat

    def glacier_stat(remote, key, **k):
        st = real_stat(remote, key, **k)
        st["tier"] = "GLACIER"
        return st

    monkeypatch.setattr(_transport, "object_stat", glacier_stat)
    do_push(plan, config=config, ledger_path=ledger, rclone_config_path=crypt_remote["config_path"])

    pre_text = _hash_file(corpus["text"])

    # Without the flag → unit is skipped (not synchronous), nothing deleted.
    results = do_evict(
        ledger, config=config, system_dir=sys_dir, rclone_config_path=crypt_remote["config_path"]
    )
    assert results == []
    assert corpus["text"].exists()
    assert _hash_file(corpus["text"]) == pre_text

    # With the flag → it evicts.
    results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        allow_cold_evict=True,
        rclone_config_path=crypt_remote["config_path"],
    )
    assert any(r.status == "purged" for r in results)
    assert not corpus["text"].exists()


# ---------------------------------------------------------------------------
# 7. Round-trip verify must actually inspect the remote .rclonelink, not just
#    rebuild the symlink from the manifest. A missing/corrupt .rclonelink on
#    the remote MUST cause evict to refuse — otherwise the "independently
#    recoverable from remote" guarantee doesn't hold for symlinks.
# ---------------------------------------------------------------------------


def test_evict_refuses_when_remote_symlink_is_missing(crypt_remote, tmp_path, monkeypatch):
    import subprocess

    src = tmp_path / "corpus"
    corpus = build_corpus(src)
    pre = _snapshot(src)
    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    plan = _build_plan(corpus, config.remote)
    push_results = do_push(
        plan,
        config=config,
        ledger_path=ledger,
        rclone_config_path=crypt_remote["config_path"],
    )

    # Simulate a partial upload: delete the .rclonelink from the remote.
    tree_entry = next(r for r in push_results if r.is_dir)
    rm_path = f"{config.remote}/{tree_entry.remote_key}/link_to_a.rclonelink"
    subprocess.run(
        ["rclone", "delete", rm_path],
        env={**os.environ, "RCLONE_CONFIG": str(crypt_remote["config_path"])},
        check=True,
    )

    # evict re-runs the round-trip verify; it must catch the missing symlink.
    # File units in the same plan have nothing wrong with them, so they will
    # successfully evict — only the dir unit (the one with the symlink) refuses.
    evict_results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        rclone_config_path=crypt_remote["config_path"],
    )
    dir_evicts = [r for r in evict_results if r.is_dir]
    assert dir_evicts, "expected at least one dir-unit evict result"
    assert all(r.status == "failed" for r in dir_evicts), [r.status for r in dir_evicts]

    # The TREE specifically — the one we corrupted — is still on disk because
    # its verify failed. That's the load-bearing claim of this test.
    assert corpus["tree"].exists()
    assert (corpus["tree"] / "inner" / "a.txt").read_text() == "inner a\n"
    assert os.readlink(corpus["tree"] / "link_to_a") == "inner/a.txt"
    # And the pre-state of the tree is byte-identical (no partial damage).
    tree_snapshot = {k: v for k, v in pre.items() if k.startswith("tree")}
    post_tree = {
        k: v for k, v in _snapshot(src).items() if k.startswith("tree")
    }
    assert post_tree == tree_snapshot


# ---------------------------------------------------------------------------
# 8. Tombstone collision: two unit_ids that fold to the same _safe_name (e.g.
#    space vs underscore) MUST NOT clobber each other's tombstones when
#    evicted in the same second.
# ---------------------------------------------------------------------------


def test_tombstone_collision_avoidance(crypt_remote, tmp_path, monkeypatch):
    src = tmp_path / "corpus"
    src.mkdir()
    # These two filenames both fold to "foo_bar" under attic's _safe_name.
    a = src / "foo bar"
    b = src / "foo_bar"
    a.write_text("contents of A")
    b.write_text("contents of B (different)")

    config = _config_for(crypt_remote)
    sys_dir = _system_dir(tmp_path)
    ledger = sys_dir / "ledgers" / "push.jsonl"
    monkeypatch.setattr(_transport, "rclone_version", lambda: "rclone v1.74.2")

    plan = _plan.build_plan_from_paths([a, b], remote=config.remote)
    push_results = do_push(
        plan,
        config=config,
        ledger_path=ledger,
        rclone_config_path=crypt_remote["config_path"],
    )
    assert all(r.status == "verified" for r in push_results)

    evict_results = do_evict(
        ledger,
        config=config,
        system_dir=sys_dir,
        rclone_config_path=crypt_remote["config_path"],
    )
    purged = [r for r in evict_results if r.status == "purged"]
    assert len(purged) == 2, [r.status for r in evict_results]

    # Both tombstone files exist with DISTINCT paths.
    tombstones = [Path(r.tombstone_path) for r in purged if r.tombstone_path]
    assert len(tombstones) == 2
    assert len({str(t) for t in tombstones}) == 2, f"path collision: {tombstones}"
    for t in tombstones:
        assert t.exists(), f"tombstone missing on disk: {t}"

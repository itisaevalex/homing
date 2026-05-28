"""attic operations — push / evict / restore logic + the recoverability gate.

Extracted from ``cli.py`` so the module stays under the repo's 800-line ceiling
and so tests can import the operation functions without the typer layer.

The whole point of attic lives here: ``do_evict`` deletes a local copy ONLY
after a REAL download -> decrypt -> re-hash round trip (``_roundtrip_verify_*``)
proves the bytes are independently recoverable under the currently-loaded crypt
config. ``rclone cryptcheck`` and rclone exit codes are NEVER trusted as the
recoverability gate.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_mod
import tempfile
import time
from pathlib import Path

from . import config as _config
from . import ledger as _ledger
from . import plan as _plan
from . import platform as _plat
from . import transport as _transport
from .config import AtticConfig, ConfigError
from .ledger import LedgerEntry
from .plan import ArchiveAction, ArchivePlan
from .transport import TransportError

__all__ = [
    "EvictionRefused",
    "do_push",
    "do_evict",
    "do_restore",
]


class EvictionRefused(Exception):
    """Raised when a safety precondition for deleting local data is not met."""


# ---------------------------------------------------------------------------
# Round-trip verification — THE trusted recoverability gate.
# ---------------------------------------------------------------------------


def _roundtrip_verify_file(
    remote: str,
    remote_key: str,
    expected_hash: str,
    *,
    rclone_config_path: Path | None,
) -> None:
    """Download a file object, hash the decrypted bytes, assert == expected.

    Raises ``EvictionRefused`` if the bytes don't round-trip. This — not any
    rclone exit code, not cryptcheck — is what proves recoverability.
    """
    with tempfile.TemporaryDirectory(prefix="attic-verify-") as tmp:
        dest = Path(tmp) / "obj"
        _transport.download_file(remote, remote_key, dest, rclone_config_path=rclone_config_path)
        got = _ledger._hash_file(dest)
    if got != expected_hash:
        raise EvictionRefused(
            f"round-trip hash mismatch for {remote_key}: " f"expected {expected_hash}, got {got}"
        )


def _roundtrip_verify_dir(
    remote: str,
    remote_prefix: str,
    manifest: list[dict],
    *,
    rclone_config_path: Path | None,
) -> None:
    """Download a dir tree, rebuild it from the manifest, assert file hashes.

    Downloads with ``--links`` (symlinks arrive as ``.rclonelink`` files),
    reconstitutes symlinks + empty dirs from the manifest, then checks every
    manifest ``file`` entry's sha256 against the downloaded bytes.
    """
    with tempfile.TemporaryDirectory(prefix="attic-verify-") as tmp:
        staged = Path(tmp) / "tree"
        _transport.download_dir(
            remote, remote_prefix, staged, rclone_config_path=rclone_config_path
        )
        _materialize_from_manifest(staged, manifest)
        _assert_manifest_matches(staged, manifest)


def _materialize_from_manifest(root: Path, manifest: list[dict]) -> None:
    """Reconstitute symlinks + empty dirs from a manifest into ``root``.

    rclone ``--links`` writes a symlink ``foo`` as ``foo.rclonelink`` (a text
    file containing the target). We turn those back into real symlinks. Empty
    dirs (dropped by object stores) are recreated. File modes are restored.
    """
    for entry in manifest:
        rel = entry["rel"]
        etype = entry["type"]
        target_path = root / rel
        if etype == "symlink":
            rclonelink = root / f"{rel}.rclonelink"
            if rclonelink.exists():
                rclonelink.unlink()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(target_path):
                target_path.unlink()
            os.symlink(entry["target"], target_path)
        elif etype == "emptydir":
            target_path.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(target_path, entry.get("mode"))
        elif etype == "file":
            _chmod_quiet(target_path, entry.get("mode"))


def _assert_manifest_matches(root: Path, manifest: list[dict]) -> None:
    """Every manifest entry must be present + byte-correct under ``root``."""
    for entry in manifest:
        rel = entry["rel"]
        etype = entry["type"]
        p = root / rel
        if etype == "file":
            if not (p.is_file() and not p.is_symlink()):
                raise EvictionRefused(f"restore verify: missing file {rel}")
            got = _ledger._hash_file(p)
            if got != entry["sha256"]:
                raise EvictionRefused(
                    f"restore verify: hash mismatch for {rel} "
                    f"(expected {entry['sha256']}, got {got})"
                )
        elif etype == "symlink":
            if not p.is_symlink():
                raise EvictionRefused(f"restore verify: missing symlink {rel}")
            if os.readlink(p) != entry["target"]:
                raise EvictionRefused(f"restore verify: symlink target drift for {rel}")
        elif etype == "emptydir":
            if not p.is_dir():
                raise EvictionRefused(f"restore verify: missing emptydir {rel}")


def _chmod_quiet(p: Path, mode: int | None) -> None:
    if mode is None:
        return
    try:
        os.chmod(p, mode)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Ledger-entry helpers
# ---------------------------------------------------------------------------


def _base_entry_kwargs(
    *,
    unit_id: str,
    op: str,
    status: str,
    source_path: str,
    is_dir: bool,
    config: AtticConfig,
    remote_key: str,
) -> dict:
    return {
        "schema_version": _ledger.LEDGER_SCHEMA_VERSION,
        "unit_id": unit_id,
        "op": op,
        "status": status,
        "source_path": source_path,
        "is_dir": is_dir,
        "remote_backend": config.remote,
        "remote_key": remote_key,
        "key_fingerprint": config.key_fingerprint,
        "local_content_hash": "",
        "manifest": None,
        "source_mode": 0o644,
        "size": 0,
        "object_count": 0,
        "remote_storage_tier": "",
        "remote_size": 0,
        "roundtrip_verified": False,
        "verify_method": config.verify_method,
        "tombstone_path": None,
        "staging_path": None,
        "timestamp": time.time(),
        "rclone_version": _safe_rclone_version(),
    }


def _safe_rclone_version() -> str:
    try:
        return _transport.rclone_version()
    except TransportError:
        return "unknown"


# ---------------------------------------------------------------------------
# do_push — upload + round-trip verify. NEVER deletes local.
# ---------------------------------------------------------------------------


def do_push(
    plan: ArchivePlan,
    *,
    config: AtticConfig,
    ledger_path: Path,
    rclone_config_path: Path | None = None,
    force: bool = False,
) -> list[LedgerEntry]:
    """Push every action's source to the remote, with round-trip verification.

    For each action: compute local hash → refuse if the remote key already
    exists (unless ``force``) → upload → round-trip verify → append a
    ``verified`` ledger entry. NEVER deletes local data. A failure on one unit
    appends ``failed`` and continues to the next.
    """
    remote = config.remote
    results: list[LedgerEntry] = []
    for action in plan.actions:
        source = Path(action.source)
        unit_id = str(source.resolve()) if source.exists() else action.source
        try:
            results.append(
                _push_one(
                    action,
                    unit_id=unit_id,
                    remote=remote,
                    config=config,
                    ledger_path=ledger_path,
                    rclone_config_path=rclone_config_path,
                    force=force,
                )
            )
        except (TransportError, EvictionRefused, OSError):
            failed = LedgerEntry(
                **{
                    **_base_entry_kwargs(
                        unit_id=unit_id,
                        op="push",
                        status="failed",
                        source_path=action.source,
                        is_dir=action.is_dir,
                        config=config,
                        remote_key=action.remote_key,
                    ),
                    "verify_method": config.verify_method,
                }
            )
            _ledger.append(ledger_path, failed)
            results.append(failed)
    return results


def _push_one(
    action: ArchiveAction,
    *,
    unit_id: str,
    remote: str,
    config: AtticConfig,
    ledger_path: Path,
    rclone_config_path: Path | None,
    force: bool,
) -> LedgerEntry:
    source = Path(action.source)
    if not source.exists() and not source.is_symlink():
        raise EvictionRefused(f"push source does not exist: {source}")

    is_dir = action.is_dir
    manifest = _ledger.build_manifest(source) if is_dir else None
    local_hash = _ledger.content_hash(source)
    size = _plan._path_size(source)
    source_mode = stat_mod.S_IMODE(source.lstat().st_mode)

    # begin
    _ledger.append(
        ledger_path,
        LedgerEntry(
            **{
                **_base_entry_kwargs(
                    unit_id=unit_id,
                    op="push",
                    status="begin",
                    source_path=action.source,
                    is_dir=is_dir,
                    config=config,
                    remote_key=action.remote_key,
                ),
                "local_content_hash": local_hash,
                "manifest": manifest,
                "source_mode": source_mode,
                "size": size,
            }
        ),
    )

    # Refuse-overwrite: pre-check existence (--immutable is a no-op for
    # differing content, so we can't rely on it).
    if is_dir:
        existing = _transport.list_objects(
            remote, action.remote_key, rclone_config_path=rclone_config_path
        )
        if existing and not force:
            raise EvictionRefused(
                f"remote prefix already has {len(existing)} object(s): "
                f"{action.remote_key} (pass --force to overwrite)"
            )
    else:
        if (
            _transport.object_exists(
                remote, action.remote_key, rclone_config_path=rclone_config_path
            )
            and not force
        ):
            raise EvictionRefused(
                f"remote key already exists: {action.remote_key} " f"(pass --force to overwrite)"
            )

    # Upload + confirm (uploaded).
    if is_dir:
        expected = sum(1 for e in (manifest or []) if e["type"] in ("file", "symlink"))
        object_count = _transport.upload_dir(
            source,
            remote,
            action.remote_key,
            expected_count=expected,
            rclone_config_path=rclone_config_path,
        )
        remote_size = 0
        tier = "STANDARD"
    else:
        _transport.upload_file(
            source, remote, action.remote_key, rclone_config_path=rclone_config_path
        )
        stat = _transport.object_stat(
            remote, action.remote_key, rclone_config_path=rclone_config_path
        )
        object_count = 1
        remote_size = stat["size"]
        tier = stat["tier"]

    _ledger.append(
        ledger_path,
        LedgerEntry(
            **{
                **_base_entry_kwargs(
                    unit_id=unit_id,
                    op="push",
                    status="uploaded",
                    source_path=action.source,
                    is_dir=is_dir,
                    config=config,
                    remote_key=action.remote_key,
                ),
                "local_content_hash": local_hash,
                "manifest": manifest,
                "source_mode": source_mode,
                "size": size,
                "object_count": object_count,
                "remote_storage_tier": tier,
                "remote_size": remote_size,
            }
        ),
    )

    # THE gate: round-trip verify the bytes are independently recoverable.
    if is_dir:
        _roundtrip_verify_dir(
            remote,
            action.remote_key,
            manifest or [],
            rclone_config_path=rclone_config_path,
        )
    else:
        _roundtrip_verify_file(
            remote,
            action.remote_key,
            local_hash,
            rclone_config_path=rclone_config_path,
        )

    verified = LedgerEntry(
        **{
            **_base_entry_kwargs(
                unit_id=unit_id,
                op="push",
                status="verified",
                source_path=action.source,
                is_dir=is_dir,
                config=config,
                remote_key=action.remote_key,
            ),
            "local_content_hash": local_hash,
            "manifest": manifest,
            "source_mode": source_mode,
            "size": size,
            "object_count": object_count,
            "remote_storage_tier": tier,
            "remote_size": remote_size,
            "roundtrip_verified": True,
        }
    )
    _ledger.append(ledger_path, verified)
    return verified


# ---------------------------------------------------------------------------
# do_evict — the only command that deletes local data.
# ---------------------------------------------------------------------------


def do_evict(
    ledger_path: Path,
    *,
    config: AtticConfig,
    system_dir: Path,
    allow_cold_evict: bool = False,
    rclone_config_path: Path | None = None,
) -> list[LedgerEntry]:
    """Free local copies of units that are proven independently recoverable.

    For each unit whose latest status is ``verified`` AND ``roundtrip_verified``
    AND key_fingerprint matches the current config AND (tier synchronous unless
    ``--allow-cold-evict``): RE-RUN the round-trip verify, write a durable
    tombstone, THEN delete the local copy. If anything fails before the
    tombstone is durably written, the local copy is left intact.
    """
    if config.key_fingerprint and not _fingerprint_matches(config, rclone_config_path):
        raise EvictionRefused(
            "crypt key/config fingerprint changed since config was written — "
            "refusing to evict (remote may be unrecoverable)"
        )

    results: list[LedgerEntry] = []
    latest = _ledger.latest_by_unit(ledger_path)
    for entry in latest.values():
        if not _is_evictable(entry, config, allow_cold_evict):
            continue
        try:
            results.append(
                _evict_one(
                    entry,
                    config=config,
                    system_dir=system_dir,
                    ledger_path=ledger_path,
                    rclone_config_path=rclone_config_path,
                )
            )
        except (TransportError, EvictionRefused, OSError):
            failed = _status_entry(entry, "evict", "failed", config)
            _ledger.append(ledger_path, failed)
            results.append(failed)
    return results


def _is_evictable(entry: LedgerEntry, config: AtticConfig, allow_cold: bool) -> bool:
    if entry.status != "verified" or not entry.roundtrip_verified:
        return False
    if config.key_fingerprint and entry.key_fingerprint != config.key_fingerprint:
        return False
    if not allow_cold and not _transport.is_synchronous_tier(entry.remote_storage_tier):
        return False
    return True


def _evict_one(
    entry: LedgerEntry,
    *,
    config: AtticConfig,
    system_dir: Path,
    ledger_path: Path,
    rclone_config_path: Path | None,
) -> LedgerEntry:
    source = Path(entry.source_path)
    if not os.path.lexists(source):
        raise EvictionRefused(f"evict source already gone: {source}")

    # RE-RUN the trusted round-trip verify against the live remote.
    if entry.is_dir:
        _roundtrip_verify_dir(
            config.remote,
            entry.remote_key,
            entry.manifest or [],
            rclone_config_path=rclone_config_path,
        )
    else:
        _roundtrip_verify_file(
            config.remote,
            entry.remote_key,
            entry.local_content_hash,
            rclone_config_path=rclone_config_path,
        )

    ts = int(time.time())
    tombstone_path = _plat.tombstones_dir(system_dir) / f"{_safe_name(entry.unit_id)}-{ts}.json"

    if entry.is_dir:
        staging = _plat.staging_dir(system_dir) / f"{_safe_name(entry.unit_id)}-{ts}"
        _stage_dir(source, staging)
        _ledger.append(
            ledger_path, _status_entry(entry, "evict", "staged", config, staging_path=str(staging))
        )
        # Tombstone must be durable BEFORE we purge the staged copy.
        _write_tombstone(tombstone_path, entry, config)
        _ledger.append(
            ledger_path,
            _status_entry(
                entry,
                "evict",
                "tombstoned",
                config,
                tombstone_path=str(tombstone_path),
                staging_path=str(staging),
            ),
        )
        shutil.rmtree(staging)
        result = _status_entry(entry, "evict", "purged", config, tombstone_path=str(tombstone_path))
        _ledger.append(ledger_path, result)
        return result

    # File unit: tombstone first (durable), then unlink.
    _write_tombstone(tombstone_path, entry, config)
    _ledger.append(
        ledger_path,
        _status_entry(entry, "evict", "tombstoned", config, tombstone_path=str(tombstone_path)),
    )
    source.unlink()
    result = _status_entry(entry, "evict", "purged", config, tombstone_path=str(tombstone_path))
    _ledger.append(ledger_path, result)
    return result


def _stage_dir(source: Path, staging: Path) -> None:
    """Move a dir tree to staging — same-FS rename, else copy-verify-rmtree."""
    staging.parent.mkdir(parents=True, exist_ok=True)
    if _same_filesystem(source, staging):
        os.rename(source, staging)
        return
    # Cross-FS: copy → verify dir-tree hash → remove source (cabinet pattern).
    shutil.copytree(source, staging, symlinks=True)
    if _ledger._hash_dir_tree(source) != _ledger._hash_dir_tree(staging):
        shutil.rmtree(staging, ignore_errors=True)
        raise EvictionRefused(f"cross-FS staging verify failed: {source}")
    shutil.rmtree(source)


def _same_filesystem(a: Path, b: Path) -> bool:
    try:
        a_dev = a.lstat().st_dev
    except OSError:
        return False
    parent = b
    for _ in range(50):
        if parent.exists():
            break
        if parent.parent == parent:
            break
        parent = parent.parent
    try:
        return a_dev == parent.lstat().st_dev
    except OSError:
        return False


def _write_tombstone(path: Path, entry: LedgerEntry, config: AtticConfig) -> None:
    """Write a durable tombstone JSON next to attic state (fsync'd)."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _ledger.LEDGER_SCHEMA_VERSION,
        "unit_id": entry.unit_id,
        "source_path": entry.source_path,
        "is_dir": entry.is_dir,
        "remote_backend": config.remote,
        "remote_key": entry.remote_key,
        "key_fingerprint": entry.key_fingerprint,
        "local_content_hash": entry.local_content_hash,
        "manifest": entry.manifest,
        "source_mode": entry.source_mode,
        "size": entry.size,
        "object_count": entry.object_count,
        "remote_storage_tier": entry.remote_storage_tier,
        "restore_command": f"attic restore {entry.unit_id} --confirmed",
        "evicted_at": time.time(),
    }
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    with path.open("wb") as fh:
        fh.write(text.encode("utf-8"))
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _status_entry(
    base: LedgerEntry,
    op: str,
    status: str,
    config: AtticConfig,
    *,
    tombstone_path: str | None = None,
    staging_path: str | None = None,
) -> LedgerEntry:
    """Clone a ledger entry with a new op/status (carrying recovery metadata)."""
    return LedgerEntry(
        schema_version=base.schema_version,
        unit_id=base.unit_id,
        op=op,
        status=status,
        source_path=base.source_path,
        is_dir=base.is_dir,
        remote_backend=config.remote,
        remote_key=base.remote_key,
        key_fingerprint=base.key_fingerprint,
        local_content_hash=base.local_content_hash,
        manifest=base.manifest,
        source_mode=base.source_mode,
        size=base.size,
        object_count=base.object_count,
        remote_storage_tier=base.remote_storage_tier,
        remote_size=base.remote_size,
        roundtrip_verified=base.roundtrip_verified,
        verify_method=config.verify_method,
        tombstone_path=tombstone_path,
        staging_path=staging_path,
        timestamp=time.time(),
        rclone_version=base.rclone_version,
    )


def _safe_name(unit_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in unit_id).strip("_")[-120:]


def _fingerprint_matches(config: AtticConfig, rclone_config_path: Path | None) -> bool:
    remote_name = config.remote.split(":", 1)[0]
    try:
        current = _config.crypt_key_fingerprint(remote_name, rclone_config_path=rclone_config_path)
    except ConfigError:
        return False
    return current == config.key_fingerprint


# ---------------------------------------------------------------------------
# do_restore — download, verify, atomic place.
# ---------------------------------------------------------------------------


def do_restore(
    entry: LedgerEntry,
    *,
    config: AtticConfig,
    ledger_path: Path,
    to: Path | None = None,
    rclone_config_path: Path | None = None,
) -> LedgerEntry:
    """Download a unit, verify hash/manifest, atomically place at the target.

    Refuses if the target exists. Refuses on key_fingerprint mismatch. Downloads
    to a temp dir under the FINAL target's parent (same-FS atomic rename), then
    renames into place only after the recovery hash check passes.
    """
    if (
        config.key_fingerprint
        and entry.key_fingerprint
        and (
            config.key_fingerprint != entry.key_fingerprint
            or not _fingerprint_matches(config, rclone_config_path)
        )
    ):
        raise EvictionRefused(
            "crypt key fingerprint mismatch — refusing to restore "
            "(remote may be unrecoverable under the current key)"
        )

    target = Path(to) if to else Path(entry.source_path)
    if os.path.lexists(target):
        raise EvictionRefused(f"restore target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    _ledger.append(ledger_path, _status_entry(entry, "restore", "begin", config))

    staging = Path(tempfile.mkdtemp(prefix=".attic-restore-", dir=str(target.parent)))
    try:
        if entry.is_dir:
            tree = staging / "tree"
            _transport.download_dir(
                config.remote,
                entry.remote_key,
                tree,
                rclone_config_path=rclone_config_path,
            )
            _materialize_from_manifest(tree, entry.manifest or [])
            _ledger.append(ledger_path, _status_entry(entry, "restore", "downloaded", config))
            _assert_manifest_matches(tree, entry.manifest or [])
            _ledger.append(ledger_path, _status_entry(entry, "restore", "verified", config))
            os.rename(tree, target)
        else:
            obj = staging / "obj"
            _transport.download_file(
                config.remote,
                entry.remote_key,
                obj,
                rclone_config_path=rclone_config_path,
            )
            _ledger.append(ledger_path, _status_entry(entry, "restore", "downloaded", config))
            got = _ledger._hash_file(obj)
            if got != entry.local_content_hash:
                raise EvictionRefused(
                    f"restore hash mismatch: expected {entry.local_content_hash}, got {got}"
                )
            _ledger.append(ledger_path, _status_entry(entry, "restore", "verified", config))
            os.rename(obj, target)
            _chmod_quiet(target, entry.source_mode)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)

    result = _status_entry(entry, "restore", "placed", config)
    _ledger.append(ledger_path, result)
    return result

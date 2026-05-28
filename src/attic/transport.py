"""rclone transport wrapper — carries the recoverability guarantee.

Pure functions taking an explicit ``remote`` (e.g. ``"attic-crypt:bucket/pre"``)
and an optional ``rclone_config_path`` for test isolation (injected via the
``RCLONE_CONFIG`` env). Every transfer/verify command passes
``--use-json-log --stats 0 --retries 1 --low-level-retries 1`` and parses stdout
JSON. ANY non-zero exit is a failure — no exit code is treated as "less serious".

This module never decides whether bytes are *recoverable*; it only moves and
inspects objects. The trusted recoverability gate (download → decrypt → re-hash)
lives in the CLI/ledger layer that calls these primitives.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# Flags every transfer/verify command carries (per spec).
_COMMON_FLAGS: tuple[str, ...] = (
    "--use-json-log",
    "--stats",
    "0",
    "--retries",
    "1",
    "--low-level-retries",
    "1",
)

# Storage tiers that are immediately readable (synchronous). GLACIER /
# DEEP_ARCHIVE / etc. require a restore step before the bytes can be read, so
# they are NOT synchronous and evict must refuse them without --allow-cold-evict.
_SYNCHRONOUS_TIERS: frozenset[str] = frozenset(
    {
        "STANDARD",
        "STANDARD_IA",
        "ONEZONE_IA",
        "INTELLIGENT_TIERING",
        "REDUCED_REDUNDANCY",
        "HOT",
        "COOL",
    }
)


class TransportError(Exception):
    """Raised when an rclone invocation fails or a post-transfer check fails."""


def _join_remote_key(remote: str, key: str) -> str:
    """Join a remote (``name:base/prefix``) and a relative key into one path.

    ``remote`` already carries the ``remote:`` scheme. We append the key under
    its base path, collapsing duplicate slashes around the join.
    """
    base = remote.rstrip("/")
    sub = key.lstrip("/")
    if not sub:
        return base
    return f"{base}/{sub}"


def _run(
    args: list[str],
    *,
    rclone_config_path: Path | None,
) -> subprocess.CompletedProcess[str]:
    """Run an rclone argv list. Returns the completed process (no exit check)."""
    env = dict(os.environ)
    if rclone_config_path is not None:
        env["RCLONE_CONFIG"] = str(rclone_config_path)
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TransportError("rclone not found on PATH") from exc


def _run_checked(args: list[str], *, rclone_config_path: Path | None) -> str:
    """Run rclone, raising ``TransportError`` on ANY non-zero exit."""
    proc = _run(args, rclone_config_path=rclone_config_path)
    if proc.returncode != 0:
        raise TransportError(
            f"rclone {' '.join(args[1:3])} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def rclone_available() -> bool:
    """Is rclone on PATH and runnable?"""
    try:
        proc = subprocess.run(
            ["rclone", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0


def rclone_version() -> str:
    """First line of ``rclone version`` (e.g. ``rclone v1.74.2``)."""
    proc = subprocess.run(
        ["rclone", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise TransportError(f"rclone version failed: {proc.stderr.strip()}")
    first = proc.stdout.splitlines()[0] if proc.stdout.strip() else ""
    return first.strip()


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _lsjson(target: str, *, recurse: bool, rclone_config_path: Path | None) -> list[dict]:
    args = ["rclone", "lsjson", *_COMMON_FLAGS]
    if recurse:
        args.append("-R")
    args.append(target)
    proc = _run(args, rclone_config_path=rclone_config_path)
    if proc.returncode != 0:
        # A not-yet-created prefix is an empty listing, not an error — every
        # other non-zero exit IS a transport failure (never swallowed).
        if "directory not found" in proc.stderr.lower() or "not found" in proc.stderr.lower():
            return []
        raise TransportError(
            f"rclone lsjson failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    out = proc.stdout
    if not out.strip():
        return []
    parsed = json.loads(out)
    if not isinstance(parsed, list):
        raise TransportError(f"unexpected lsjson output for {target}")
    return parsed


def object_exists(
    remote: str,
    remote_key: str,
    *,
    rclone_config_path: Path | None = None,
) -> bool:
    """Does an object exist at ``remote_key``?

    Uses ``lsjson --stat``: a present object yields a JSON object; an absent one
    yields ``null``/empty. A non-zero exit (other than the missing case) is a
    transport error — we DON'T silently treat failure as "absent".
    """
    target = _join_remote_key(remote, remote_key)
    args = ["rclone", "lsjson", "--stat", *_COMMON_FLAGS, target]
    proc = _run(args, rclone_config_path=rclone_config_path)
    if proc.returncode != 0:
        # rclone returns non-zero when the parent directory itself is absent.
        # Treat "directory not found"/"not found" as a clean "does not exist".
        stderr = proc.stderr.lower()
        if "not found" in stderr or "directory not found" in stderr:
            return False
        raise TransportError(
            f"object_exists check failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    out = proc.stdout.strip()
    if not out or out == "null":
        return False
    parsed = json.loads(out)
    return parsed is not None


def object_stat(
    remote: str,
    remote_key: str,
    *,
    rclone_config_path: Path | None = None,
) -> dict:
    """Stat a single object → ``{size, modtime, tier, name}``.

    Raises ``TransportError`` if the object is absent — callers that want a
    soft check use ``object_exists`` first.
    """
    target = _join_remote_key(remote, remote_key)
    out = _run_checked(
        ["rclone", "lsjson", "--stat", *_COMMON_FLAGS, target],
        rclone_config_path=rclone_config_path,
    )
    stripped = out.strip()
    if not stripped or stripped == "null":
        raise TransportError(f"object not found: {target}")
    obj = json.loads(stripped)
    return {
        "name": obj.get("Name"),
        "size": int(obj.get("Size", -1)),
        "modtime": obj.get("ModTime"),
        # Local backend (and many others) don't set Tier — absence → STANDARD.
        "tier": obj.get("Tier") or "STANDARD",
        "is_dir": bool(obj.get("IsDir", False)),
    }


def list_objects(
    remote: str,
    remote_prefix: str,
    *,
    rclone_config_path: Path | None = None,
) -> list[dict]:
    """Recursively list objects (files only) under ``remote_prefix``."""
    target = _join_remote_key(remote, remote_prefix)
    entries = _lsjson(target, recurse=True, rclone_config_path=rclone_config_path)
    return [e for e in entries if not e.get("IsDir", False)]


def storage_tier(
    remote: str,
    remote_key: str,
    *,
    rclone_config_path: Path | None = None,
) -> str:
    """Storage class of an object. Missing Tier → ``"STANDARD"``."""
    return object_stat(remote, remote_key, rclone_config_path=rclone_config_path)["tier"]


def is_synchronous_tier(tier: str) -> bool:
    """Is this tier immediately readable (no async restore needed)?

    STANDARD/STANDARD_IA/etc → True. GLACIER/DEEP_ARCHIVE/ARCHIVE/etc → False.
    Unknown tiers are treated as NON-synchronous (fail safe — refuse the evict).
    """
    return tier.upper() in _SYNCHRONOUS_TIERS


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def upload_file(
    local: Path,
    remote: str,
    remote_key: str,
    *,
    rclone_config_path: Path | None = None,
) -> None:
    """Upload a single file with ``copyto``; confirm 1 object of matching size."""
    if not local.is_file():
        raise TransportError(f"upload_file source is not a file: {local}")
    expected_size = local.stat().st_size
    target = _join_remote_key(remote, remote_key)
    _run_checked(
        ["rclone", "copyto", str(local), target, *_COMMON_FLAGS],
        rclone_config_path=rclone_config_path,
    )
    stat = object_stat(remote, remote_key, rclone_config_path=rclone_config_path)
    if stat["size"] != expected_size:
        raise TransportError(
            f"post-upload size mismatch for {target}: "
            f"expected {expected_size}, got {stat['size']}"
        )


def upload_dir(
    local_dir: Path,
    remote: str,
    remote_prefix: str,
    *,
    expected_count: int | None = None,
    rclone_config_path: Path | None = None,
) -> int:
    """Upload a directory tree with ``copy --links``; confirm object count.

    ``--links`` turns symlinks into ``.rclonelink`` files so they survive an
    object-store round trip. Empty dirs are NOT preserved by object stores —
    those are reproduced from the ledger manifest, not from transport. Returns
    the uploaded object count.
    """
    if not local_dir.is_dir():
        raise TransportError(f"upload_dir source is not a directory: {local_dir}")
    target = _join_remote_key(remote, remote_prefix)
    _run_checked(
        ["rclone", "copy", "--links", str(local_dir), target, *_COMMON_FLAGS],
        rclone_config_path=rclone_config_path,
    )
    objects = list_objects(remote, remote_prefix, rclone_config_path=rclone_config_path)
    count = len(objects)
    if expected_count is not None and count != expected_count:
        raise TransportError(
            f"post-upload object count mismatch under {target}: "
            f"expected {expected_count}, got {count}"
        )
    return count


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_file(
    remote: str,
    remote_key: str,
    local_dest: Path,
    *,
    rclone_config_path: Path | None = None,
) -> None:
    """Download a single object to ``local_dest`` with ``copyto``."""
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    target = _join_remote_key(remote, remote_key)
    _run_checked(
        ["rclone", "copyto", target, str(local_dest), *_COMMON_FLAGS],
        rclone_config_path=rclone_config_path,
    )
    if not local_dest.is_file():
        raise TransportError(f"download produced no file at {local_dest}")


def download_dir(
    remote: str,
    remote_prefix: str,
    local_dest_dir: Path,
    *,
    rclone_config_path: Path | None = None,
) -> None:
    """Download a directory tree to ``local_dest_dir`` with ``copy --links``.

    ``--links`` reconstructs ``.rclonelink`` files; the caller reconstitutes
    them into real symlinks (and recreates empty dirs) from the manifest.
    """
    local_dest_dir.mkdir(parents=True, exist_ok=True)
    target = _join_remote_key(remote, remote_prefix)
    _run_checked(
        ["rclone", "copy", "--links", target, str(local_dest_dir), *_COMMON_FLAGS],
        rclone_config_path=rclone_config_path,
    )

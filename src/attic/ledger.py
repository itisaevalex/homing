"""attic ledger — append-only JSONL with fsync discipline + dir manifests.

Reuses cabinet's symlink/dir-hash policy: ``_hash_dir_tree``, ``_fingerprint``,
``FileFingerprint`` are imported from ``cabinet.undo`` so attic's scalar hashes
match cabinet's byte-for-byte. The ledger schema is attic-specific (push/evict/
restore state machines), so ``LedgerEntry`` is defined here.

A directory unit's manifest records, per entry, ``rel`` + ``type`` (file |
symlink | emptydir) + ``sha256``/``target`` + ``size`` + ``mode``. This is how
empty dirs and symlinks survive an object-store round trip — they are
reproduced from the manifest on restore, not from rclone transport (object
stores drop empty dirs; ``--links`` turns symlinks into ``.rclonelink`` files).
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path

# Reuse cabinet's never-follow-symlink hashing policy verbatim — do NOT
# reimplement it here. attic's correctness depends on hashing identically.
from cabinet.undo import (  # noqa: F401  (FileFingerprint re-exported for callers)
    FileFingerprint,
    _fingerprint,
    _hash_dir_tree,
    _hash_file,
)

LEDGER_SCHEMA_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One append-only ledger entry describing a push/evict/restore step.

    Append-only: a state machine advances by appending new entries that share a
    ``unit_id``; the latest entry for a unit is its live status. Statuses:
      push:    begin | uploaded | verified | failed
      evict:   begin | staged | tombstoned | purged | failed
      restore: begin | downloaded | verified | placed | failed
    """

    schema_version: int
    unit_id: str  # canonical absolute source path
    op: str  # "push" | "evict" | "restore"
    status: str
    source_path: str
    is_dir: bool
    remote_backend: str
    remote_key: str
    key_fingerprint: str
    local_content_hash: str  # sha256 hex (file) or "dir-tree:<hash>" (dir)
    manifest: list[dict] | None  # dirs only; else None
    source_mode: int  # POSIX file mode of a file unit (dirs carry per-entry modes in manifest)
    size: int
    object_count: int
    remote_storage_tier: str
    remote_size: int
    roundtrip_verified: bool
    verify_method: str
    tombstone_path: str | None
    staging_path: str | None
    timestamp: float
    rclone_version: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_dict(d: dict) -> LedgerEntry:
        return LedgerEntry(**d)


# ---------------------------------------------------------------------------
# Append-only JSONL I/O — same fsync discipline as cabinet._append_ledger.
# ---------------------------------------------------------------------------


def append(ledger_path: Path, entry: LedgerEntry) -> None:
    """Append one JSON-Lines entry. Flushes + fsyncs to survive crashes."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = entry.to_json() + "\n"
    with ledger_path.open("ab") as fh:
        fh.write(line.encode("utf-8"))
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Some filesystems / test doubles don't support fsync — that's a
            # platform property, not a correctness issue we can fix here.
            pass


def read(ledger_path: Path) -> list[LedgerEntry]:
    """Load all ledger entries in append order."""
    if not ledger_path.exists():
        return []
    out: list[LedgerEntry] = []
    with ledger_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            out.append(LedgerEntry.from_dict(json.loads(line)))
    return out


def latest_by_unit(ledger_path: Path) -> dict[str, LedgerEntry]:
    """Latest entry per ``unit_id`` (its live status), in append order."""
    latest: dict[str, LedgerEntry] = {}
    for entry in read(ledger_path):
        latest[entry.unit_id] = entry
    return latest


# ---------------------------------------------------------------------------
# Directory manifest — the empty-dir + symlink survival mechanism.
# ---------------------------------------------------------------------------


def build_manifest(path: Path) -> list[dict]:
    """Walk ``path`` (never following symlinks) → a sorted manifest.

    Each entry is one of:
      - ``{rel, type: "file",     sha256, size, mode}``
      - ``{rel, type: "symlink",  target,       mode}``
      - ``{rel, type: "emptydir",               mode}``

    Empty dirs and symlinks are recorded explicitly because object stores drop
    empty dirs and ``rclone --links`` rewrites symlinks as ``.rclonelink``
    files — both are reproduced from this manifest on restore. The traversal
    uses ``os.walk(followlinks=False)`` for the same deterministic,
    version-independent behaviour cabinet relies on.
    """
    entries: list[dict] = []
    for root, dirs, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        filenames.sort()

        kept_dirs: list[str] = []
        for name in dirs:
            entry = root_path / name
            rel = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                entries.append(
                    {
                        "rel": rel,
                        "type": "symlink",
                        "target": os.readlink(entry),
                        "mode": _lmode(entry),
                    }
                )
            else:
                kept_dirs.append(name)
                if _is_empty_dir(entry):
                    entries.append({"rel": rel, "type": "emptydir", "mode": _lmode(entry)})
        dirs[:] = kept_dirs

        for name in filenames:
            entry = root_path / name
            rel = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                entries.append(
                    {
                        "rel": rel,
                        "type": "symlink",
                        "target": os.readlink(entry),
                        "mode": _lmode(entry),
                    }
                )
            elif entry.is_file():
                st = entry.lstat()
                entries.append(
                    {
                        "rel": rel,
                        "type": "file",
                        "sha256": _hash_file(entry),
                        "size": st.st_size,
                        "mode": stat_mod.S_IMODE(st.st_mode),
                    }
                )
            # Special files (sockets, fifos, devices) don't migrate — skip.

    entries.sort(key=lambda e: (e["rel"], e["type"]))
    return entries


def _is_empty_dir(p: Path) -> bool:
    try:
        next(p.iterdir())
        return False
    except StopIteration:
        return True
    except OSError:
        return False


def _lmode(p: Path) -> int:
    return stat_mod.S_IMODE(p.lstat().st_mode)


def content_hash(path: Path) -> str:
    """sha256 hex for a file; ``dir-tree:<hash>`` for a directory.

    Delegates to cabinet's hashers so attic's hashes match cabinet's exactly.
    """
    if path.is_dir() and not path.is_symlink():
        return _hash_dir_tree(path)
    return _hash_file(path)

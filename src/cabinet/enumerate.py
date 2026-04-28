"""Phase A — walk + initial metadata.

Pure read-only enumeration of folder + file metadata. Idempotent: same input
produces byte-identical output (modulo timestamps that are part of the data).

The output is the load-bearing structure for the rest of cabinet: homogeneity
scoring, sampling, classification, and triage all consume `EnumerationResult`.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Hardcoded prune list — directories cabinet should never descend into.
# Comes straight out of CLAUDE.md hard rules: cabinet reads $HOME paths the user
# points it at, but skips machinery directories that are not user data.
DEFAULT_PRUNE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".cache",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".idea",
        ".vscode",
        ".gradle",
        ".m2",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".DS_Store",
        ".Trash",
        ".Trashes",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)

# Files larger than this get a sentinel "size-only" hash. Hashing 4GB ISOs is
# not the right cost trade-off for triage. Dedup-by-size approximation is good
# enough at this stage; the classifier can re-hash on demand.
HASH_SIZE_LIMIT_BYTES: int = 50 * 1024 * 1024  # 50 MB
_HASH_CHUNK: int = 1024 * 1024  # 1 MB read chunks


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Metadata for a single file. Immutable."""

    path: str  # absolute, posix-style
    size: int
    mtime: float
    extension: str  # lowercase, no leading dot; "" if none
    content_hash: str  # hex sha256, or "size-only:<bytes>" sentinel

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": self.mtime,
            "extension": self.extension,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class FolderMeta:
    """Metadata for a single folder (non-recursive aggregation of its files)."""

    path: str
    depth: int
    file_count: int
    total_size: int
    file_extensions: dict[str, int]  # extension -> count, sorted by key
    date_range: tuple[float, float] | None  # (min_mtime, max_mtime), None if empty
    files: tuple[FileMeta, ...]  # sorted by path

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "depth": self.depth,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "file_extensions": dict(self.file_extensions),
            "date_range": list(self.date_range) if self.date_range else None,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass(frozen=True, slots=True)
class EnumerationResult:
    """Aggregate result of walking one or more roots."""

    roots: tuple[str, ...]
    folders: tuple[FolderMeta, ...]  # sorted by path
    skipped: tuple[str, ...]  # paths skipped (errors, prunes)

    @property
    def total_folders(self) -> int:
        return len(self.folders)

    @property
    def total_files(self) -> int:
        return sum(f.file_count for f in self.folders)

    @property
    def total_size(self) -> int:
        return sum(f.total_size for f in self.folders)

    def to_dict(self) -> dict:
        return {
            "roots": list(self.roots),
            "folders": [f.to_dict() for f in self.folders],
            "skipped": list(self.skipped),
            "totals": {
                "folders": self.total_folders,
                "files": self.total_files,
                "size_bytes": self.total_size,
            },
        }


def _hash_file(path: Path, size: int) -> str:
    """Hash a file's content with sha256, or return a size-only sentinel.

    Files above ``HASH_SIZE_LIMIT_BYTES`` get ``size-only:<bytes>`` so dedup
    can still detect same-size candidates without paying the I/O cost.
    """
    if size > HASH_SIZE_LIMIT_BYTES:
        return f"size-only:{size}"
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                h.update(chunk)
    except OSError:
        # Permission denied / vanished mid-walk — fall back to size sentinel.
        return f"size-only:{size}"
    return h.hexdigest()


def _file_meta(path: Path) -> FileMeta | None:
    """Build FileMeta for a single file. Returns None on stat failure."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not _is_regular_file(st.st_mode):
        return None
    ext = path.suffix.lower().lstrip(".")
    return FileMeta(
        path=str(path),
        size=st.st_size,
        mtime=st.st_mtime,
        extension=ext,
        content_hash=_hash_file(path, st.st_size),
    )


def _is_regular_file(mode: int) -> bool:
    import stat as stat_mod

    return stat_mod.S_ISREG(mode)


def _depth_from(root: Path, current: Path) -> int:
    try:
        rel = current.relative_to(root)
    except ValueError:
        return 0
    if str(rel) == ".":
        return 0
    return len(rel.parts)


def _should_prune(name: str, prune: frozenset[str]) -> bool:
    return name in prune


def enumerate_paths(
    paths: Iterable[Path | str],
    *,
    max_depth: int = 8,
    prune: frozenset[str] = DEFAULT_PRUNE_DIRS,
) -> EnumerationResult:
    """Walk the given paths and return a flat list of folder/file metadata.

    - Read-only. Never modifies the filesystem.
    - Idempotent. Sort everything by path so output is byte-stable.
    - Prunes well-known machinery directories.
    - Files >50MB get a size-only hash sentinel.
    - ``max_depth`` is measured from each root (root itself is depth 0).
    """
    roots: list[Path] = []
    for p in paths:
        rp = Path(p).expanduser().resolve()
        roots.append(rp)

    folders: list[FolderMeta] = []
    skipped: list[str] = []
    seen_folders: set[str] = set()

    for root in roots:
        if not root.exists():
            skipped.append(str(root))
            continue
        if not root.is_dir():
            # A bare file as input — wrap its parent as a folder unit.
            parent = root.parent
            fm = _file_meta(root)
            if fm is None:
                skipped.append(str(root))
                continue
            folder = _build_folder(parent, depth=0, files=[fm])
            if folder.path not in seen_folders:
                folders.append(folder)
                seen_folders.add(folder.path)
            continue

        # os.walk gives us topdown so we can prune in-place.
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            current = Path(dirpath)
            depth = _depth_from(root, current)

            # Prune: drop machinery dirs and respect max_depth on descent.
            kept = []
            for d in dirnames:
                if _should_prune(d, prune):
                    skipped.append(str(current / d))
                    continue
                if depth + 1 > max_depth:
                    skipped.append(str(current / d))
                    continue
                kept.append(d)
            # In-place modify so os.walk respects our pruning.
            dirnames[:] = sorted(kept)

            # Build file metas for this folder only.
            file_metas: list[FileMeta] = []
            for fname in filenames:
                fpath = current / fname
                # Skip symlinks to avoid double-counting / cycles.
                try:
                    if fpath.is_symlink():
                        skipped.append(str(fpath))
                        continue
                except OSError:
                    skipped.append(str(fpath))
                    continue
                fm = _file_meta(fpath)
                if fm is None:
                    skipped.append(str(fpath))
                    continue
                file_metas.append(fm)

            folder = _build_folder(current, depth=depth, files=file_metas)
            if folder.path not in seen_folders:
                folders.append(folder)
                seen_folders.add(folder.path)

    folders.sort(key=lambda f: f.path)
    skipped.sort()

    return EnumerationResult(
        roots=tuple(sorted(str(r) for r in roots)),
        folders=tuple(folders),
        skipped=tuple(skipped),
    )


def _build_folder(path: Path, *, depth: int, files: list[FileMeta]) -> FolderMeta:
    """Aggregate a list of FileMeta into a FolderMeta. Sorted, immutable."""
    files_sorted = tuple(sorted(files, key=lambda f: f.path))
    ext_counter: Counter[str] = Counter(f.extension for f in files_sorted)
    # Sort extensions by key for determinism.
    ext_dict = dict(sorted(ext_counter.items()))

    if files_sorted:
        mtimes = [f.mtime for f in files_sorted]
        date_range: tuple[float, float] | None = (min(mtimes), max(mtimes))
    else:
        date_range = None

    total_size = sum(f.size for f in files_sorted)

    return FolderMeta(
        path=str(path),
        depth=depth,
        file_count=len(files_sorted),
        total_size=total_size,
        file_extensions=ext_dict,
        date_range=date_range,
        files=files_sorted,
    )


def folder_by_path(result: EnumerationResult, path: str | Path) -> FolderMeta | None:
    """Lookup helper — folder metadata by absolute path."""
    target = str(Path(path).expanduser().resolve())
    for f in result.folders:
        if f.path == target:
            return f
    return None

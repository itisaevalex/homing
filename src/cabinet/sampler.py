"""Phase A — pick K representative files from a folder.

This is where the cost discipline lives: instead of reading every file in a
10,000-photo folder, the classifier reads K samples chosen here. If sampling
picks badly, the verdict is wrong. If sampling picks well, $$ stays bounded.

Strategies:
- ``stratified`` (default): first, last, middle, plus the two largest. Designed
  to surface outliers so we don't classify a "vacation photos" folder by its
  100 IMG_*.jpg files and miss the random PDF someone dropped in.
- ``random``: uniform random with a fixed seed (deterministic per-folder).
- ``by_extension``: one file per dominant extension, up to K total.
"""

from __future__ import annotations

import hashlib
import random
from typing import Literal

from .enumerate import FileMeta, FolderMeta

Strategy = Literal["stratified", "random", "by_extension"]

_DEFAULT_SEED_NS = "cabinet.sampler.v1"


def sample_files(
    folder: FolderMeta,
    k: int = 5,
    *,
    strategy: Strategy = "stratified",
    seed: int | None = None,
) -> list[FileMeta]:
    """Pick up to K representative files from ``folder``.

    Returns FileMeta objects (sorted by path) so callers can map back into
    the worklist. Always returns at most ``k`` items, possibly fewer if the
    folder has fewer files.
    """
    if k <= 0:
        return []
    files = list(folder.files)
    if not files:
        return []
    if len(files) <= k:
        return sorted(files, key=lambda f: f.path)

    if strategy == "stratified":
        picked = _stratified(files, k)
    elif strategy == "random":
        picked = _random(files, k, folder=folder, seed=seed)
    elif strategy == "by_extension":
        picked = _by_extension(files, k)
    else:
        raise ValueError(f"unknown sampling strategy: {strategy!r}")

    # De-dupe (a file might be both extreme size AND middle index) and sort.
    seen: set[str] = set()
    unique: list[FileMeta] = []
    for f in picked:
        if f.path in seen:
            continue
        seen.add(f.path)
        unique.append(f)
    unique.sort(key=lambda f: f.path)
    return unique[:k]


def _stratified(files: list[FileMeta], k: int) -> list[FileMeta]:
    """First, last, middle, plus the two largest by size."""
    n = len(files)
    by_path = sorted(files, key=lambda f: f.path)
    by_size = sorted(files, key=lambda f: (-f.size, f.path))

    picks: list[FileMeta] = []

    # Spread positional picks: first, last, middle, then quartiles, etc.
    positions = _spread_positions(n, k)
    for idx in positions:
        picks.append(by_path[idx])
        if len(picks) >= k:
            break

    # Make sure the top-2 by size are included to surface outliers.
    for i in range(min(2, len(by_size))):
        if len(picks) >= k:
            break
        picks.append(by_size[i])

    return picks[:k]


def _spread_positions(n: int, k: int) -> list[int]:
    """Pick up to k indices spread across [0, n-1]: first, last, middle, ..."""
    if n == 0:
        return []
    if k >= n:
        return list(range(n))
    # Always include first, last, middle; then fill in evenly.
    base = {0, n - 1, n // 2}
    if len(base) < k:
        # Fill with quartile-ish positions.
        denom = max(2, k)
        for i in range(1, denom):
            idx = (i * (n - 1)) // denom
            base.add(idx)
            if len(base) >= k:
                break
    return sorted(base)[:k]


def _random(
    files: list[FileMeta],
    k: int,
    *,
    folder: FolderMeta,
    seed: int | None,
) -> list[FileMeta]:
    """Deterministic uniform random sample.

    Seed is derived from the folder path if not provided so that re-runs are
    byte-stable per folder.
    """
    rng = random.Random(seed if seed is not None else _seed_for(folder.path))
    return rng.sample(files, k)


def _by_extension(files: list[FileMeta], k: int) -> list[FileMeta]:
    """One representative per extension (most common first)."""
    by_ext: dict[str, list[FileMeta]] = {}
    for f in files:
        by_ext.setdefault(f.extension, []).append(f)
    # Sort extensions by frequency desc, then alphabetical for determinism.
    ordered = sorted(by_ext.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    picks: list[FileMeta] = []
    for _ext, group in ordered:
        # Pick the first file alphabetically as a stable representative.
        group.sort(key=lambda f: f.path)
        picks.append(group[0])
        if len(picks) >= k:
            break
    # If we still have budget, fill with second-files from the largest groups.
    if len(picks) < k:
        leftovers: list[FileMeta] = []
        for _ext, group in ordered:
            if len(group) > 1:
                leftovers.extend(group[1:])
        leftovers.sort(key=lambda f: f.path)
        for f in leftovers:
            if len(picks) >= k:
                break
            picks.append(f)
    return picks[:k]


def _seed_for(path: str) -> int:
    digest = hashlib.sha256(f"{_DEFAULT_SEED_NS}:{path}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)

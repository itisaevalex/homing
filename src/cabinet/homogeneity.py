"""Phase A — score folder coherence.

Decides whether a folder is homogeneous enough to classify as a single unit
(`folder-classifiable`), needs to be broken down (`subdivide`), or should be
classified file-by-file (`per-file`).

This module is load-bearing for cost discipline (see CLAUDE.md hard rule #4):
the homogeneity score is what tells the classifier "you can sample 5 files and
trust the verdict" vs "you have to look at every file."

The score is a weighted average of four signals:

| Signal              | Weight | Range  | Notes                                      |
|---------------------|--------|--------|--------------------------------------------|
| extension_consistency | 0.35 | [0, 1] | % of files with the dominant extension     |
| filename_pattern    | 0.30   | [0, 1] | fraction matching a shared regex pattern   |
| size_coherence      | 0.15   | [0, 1] | 1 - clamped coefficient of variation       |
| date_coherence      | 0.20   | [0, 1] | narrow mtime range = 1, year+ range = 0    |

Special cases:
- Empty folders score 0.0 (verdict: per-file — there's nothing to classify).
- Folders with <3 files are "trivially homogeneous" (score 1.0, folder-classifiable).
- Folders with >500 files require the score to clear a higher bar (0.90) to
  avoid hallucinated classifications on long-tail mixed dirs (CLAUDE.md #4).

Threshold:
- score >= 0.85 → folder-classifiable
- 0.50 <= score < 0.85 → subdivide (try grouping by extension or pattern)
- score < 0.50 → per-file
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Literal

from .enumerate import FolderMeta

# Weights — must sum to 1.0. Documented above.
W_EXTENSION = 0.35
W_PATTERN = 0.30
W_SIZE = 0.15
W_DATE = 0.20

FOLDER_CLASSIFIABLE_THRESHOLD = 0.85
SUBDIVIDE_THRESHOLD = 0.50
LARGE_FOLDER_THRESHOLD = 500
LARGE_FOLDER_BAR = 0.90
TRIVIAL_FILE_COUNT = 3

# Year in seconds — used to normalize the date-range signal.
_ONE_YEAR_SECONDS = 365 * 24 * 3600
_ONE_DAY_SECONDS = 24 * 3600

# Known filename patterns. Order matters — first match wins for the dominant
# pattern. Each is an anchored regex (full-name match minus extension).
_FILENAME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("camera_dsc", re.compile(r"^DSC[_-]?\d{3,6}$", re.IGNORECASE)),
    ("camera_img", re.compile(r"^IMG[_-]?\d{3,6}$", re.IGNORECASE)),
    ("phone_iphone", re.compile(r"^IMG_\d{4}$", re.IGNORECASE)),
    ("scan_numbered", re.compile(r"^scan[_-]?\d{2,5}$", re.IGNORECASE)),
    ("photo_numbered", re.compile(r"^photo[_-]?\d{2,5}$", re.IGNORECASE)),
    ("date_prefix", re.compile(r"^\d{4}[-_]\d{2}[-_]\d{2}.*$")),
    ("screenshot", re.compile(r"^(screen ?shot|screenshot)[ _-].+$", re.IGNORECASE)),
    (
        "generic_numbered",
        # e.g. "report_001", "page-12", "track 3"
        re.compile(r"^[A-Za-z][A-Za-z _-]{1,30}[ _-]\d{1,5}$"),
    ),
)


Verdict = Literal["folder-classifiable", "subdivide", "per-file"]


@dataclass(frozen=True, slots=True)
class HomogeneityScore:
    """Result of scoring a single folder."""

    score: float  # [0.0, 1.0]
    verdict: Verdict
    evidence: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "verdict": self.verdict,
            "evidence": self.evidence,
        }


def score_folder(folder: FolderMeta) -> HomogeneityScore:
    """Compute the homogeneity score for a single folder.

    Pure function — no I/O, no mutation. Determined entirely by ``folder``.
    """
    file_count = folder.file_count

    # Special case: empty folder. Nothing to classify.
    if file_count == 0:
        return HomogeneityScore(
            score=0.0,
            verdict="per-file",
            evidence={"reason": "empty folder", "file_count": 0},
        )

    # Special case: trivially homogeneous (1-2 files).
    if file_count < TRIVIAL_FILE_COUNT:
        return HomogeneityScore(
            score=1.0,
            verdict="folder-classifiable",
            evidence={
                "reason": "trivially homogeneous (fewer than 3 files)",
                "file_count": file_count,
            },
        )

    ext_score, ext_evidence = _extension_consistency(folder)
    pattern_score, pattern_evidence = _filename_pattern_coherence(folder)
    size_score, size_evidence = _size_coherence(folder)
    date_score, date_evidence = _date_coherence(folder)

    raw_score = (
        W_EXTENSION * ext_score
        + W_PATTERN * pattern_score
        + W_SIZE * size_score
        + W_DATE * date_score
    )
    score = max(0.0, min(1.0, raw_score))

    verdict = _verdict_for(score, file_count)

    evidence = {
        "file_count": file_count,
        "extension": ext_evidence,
        "filename_pattern": pattern_evidence,
        "size": size_evidence,
        "date": date_evidence,
        "weights": {
            "extension": W_EXTENSION,
            "filename_pattern": W_PATTERN,
            "size": W_SIZE,
            "date": W_DATE,
        },
        "components": {
            "extension": round(ext_score, 4),
            "filename_pattern": round(pattern_score, 4),
            "size": round(size_score, 4),
            "date": round(date_score, 4),
        },
    }
    return HomogeneityScore(score=score, verdict=verdict, evidence=evidence)


def _verdict_for(score: float, file_count: int) -> Verdict:
    """Apply thresholds, with a higher bar for very large folders."""
    bar = LARGE_FOLDER_BAR if file_count > LARGE_FOLDER_THRESHOLD else FOLDER_CLASSIFIABLE_THRESHOLD
    if score >= bar:
        return "folder-classifiable"
    if score >= SUBDIVIDE_THRESHOLD:
        return "subdivide"
    return "per-file"


def _extension_consistency(folder: FolderMeta) -> tuple[float, dict[str, Any]]:
    """Fraction of files sharing the dominant extension."""
    counts = folder.file_extensions
    total = folder.file_count
    if total == 0 or not counts:
        return 0.0, {"dominant": None, "share": 0.0, "distinct": 0}
    dominant_ext, dominant_count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    share = dominant_count / total
    return share, {
        "dominant": dominant_ext,
        "share": round(share, 4),
        "distinct": len(counts),
        "counts": dict(counts),
    }


def _filename_pattern_coherence(folder: FolderMeta) -> tuple[float, dict[str, Any]]:
    """Fraction of filenames matching a known/inferred pattern.

    Two strategies are tried; the higher-coverage one wins:
    1. Known regex patterns (DSC_NNNN, IMG_NNNN, scan_NN, etc.).
    2. Shared longest common prefix of length >=3 chars.
    """
    files = folder.files
    total = len(files)
    if total == 0:
        return 0.0, {"strategy": None, "match": 0.0}

    stems = [_stem(f.path) for f in files]

    # Strategy 1: known patterns — pick the pattern with most matches.
    best_pattern_name: str | None = None
    best_pattern_hits = 0
    for name, pat in _FILENAME_PATTERNS:
        hits = sum(1 for s in stems if pat.match(s))
        if hits > best_pattern_hits:
            best_pattern_name = name
            best_pattern_hits = hits

    pattern_share = best_pattern_hits / total

    # Strategy 2: shared prefix coherence.
    prefix = _common_prefix(stems)
    if len(prefix) >= 3:
        prefix_hits = sum(1 for s in stems if s.startswith(prefix))
        prefix_share = prefix_hits / total
    else:
        prefix = ""
        prefix_share = 0.0

    if pattern_share >= prefix_share:
        return pattern_share, {
            "strategy": "regex",
            "pattern": best_pattern_name,
            "match": round(pattern_share, 4),
        }
    return prefix_share, {
        "strategy": "common_prefix",
        "prefix": prefix,
        "match": round(prefix_share, 4),
    }


def _size_coherence(folder: FolderMeta) -> tuple[float, dict[str, Any]]:
    """1 - clamped coefficient of variation of file sizes.

    All-same-size → 1.0. Highly varied → 0.0.
    """
    sizes = [f.size for f in folder.files]
    if len(sizes) < 2:
        return 1.0, {"reason": "fewer than 2 files", "cv": 0.0}
    mean = statistics.fmean(sizes)
    if mean == 0:
        return 1.0, {"reason": "all zero-byte", "cv": 0.0}
    stdev = statistics.pstdev(sizes)
    cv = stdev / mean
    # Clamp CV to [0, 2] then invert and rescale to [0, 1].
    # CV=0 → 1.0, CV=1 → 0.5, CV>=2 → 0.0.
    score = max(0.0, 1.0 - (cv / 2.0))
    return score, {
        "cv": round(cv, 4),
        "mean": round(mean, 2),
        "stdev": round(stdev, 2),
    }


def _date_coherence(folder: FolderMeta) -> tuple[float, dict[str, Any]]:
    """Narrow mtime range -> 1.0; year-or-more spread -> 0.0."""
    if folder.date_range is None or folder.file_count < 2:
        return 1.0, {"reason": "fewer than 2 files", "range_seconds": 0}
    lo, hi = folder.date_range
    span = max(0.0, hi - lo)
    if span <= _ONE_DAY_SECONDS:
        score = 1.0
    elif span >= _ONE_YEAR_SECONDS:
        score = 0.0
    else:
        # Linear between 1 day and 1 year.
        score = 1.0 - (span - _ONE_DAY_SECONDS) / (_ONE_YEAR_SECONDS - _ONE_DAY_SECONDS)
    return max(0.0, min(1.0, score)), {
        "range_seconds": round(span, 1),
        "range_days": round(span / _ONE_DAY_SECONDS, 2),
    }


def _stem(path_str: str) -> str:
    """Return the filename stem (no directory, no final extension)."""
    name = path_str.rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


def _common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    s_min = min(strings)
    s_max = max(strings)
    out = []
    for a, b in zip(s_min, s_max):
        if a != b:
            break
        out.append(a)
    return "".join(out)

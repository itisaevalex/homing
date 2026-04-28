"""Filename-pattern rule.

Some filenames carry a strong, machine-recognisable signal: scan_0042.pdf,
IMG_1234.jpg, "Screenshot from 2024-03-15 14-22-01.png", CV_v3.pdf. We
match a curated list of patterns against the unit's path (file unit) or
sample paths (folder unit) and classify on the dominant pattern.

We deliberately keep the pattern list short. The LLM can pick up the long
tail; this rule is for the matches we'd be embarrassed to send a vision
call about.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .base import Classification, Rule, UnitContext

# Each pattern fires against the *filename* (not full path). Order matters
# only for evidence-citation aesthetics; collisions are rare.
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "scan_NNNN",
        re.compile(r"^scan[_-]?\d{2,6}\.(pdf|jpg|jpeg|png|tiff)$", re.IGNORECASE),
        "scan-of-document",
    ),
    (
        "IMG_NNNN / DSC_NNNN / P_NNNNNNN",
        re.compile(r"^(IMG|DSC|DSCN|P|PIC|PXL|PHOTO)[_-]?\d{3,8}\.(jpg|jpeg|heic|heif|raw|dng|cr2|nef|png)$", re.IGNORECASE),
        "raw-camera",
    ),
    (
        "Screenshot from YYYY-MM-DD",
        re.compile(
            r"^(screen[\s_-]?shot|screenshot|cleanshot)[\s_-]?(from\s+)?\d{4}[-_]\d{1,2}[-_]\d{1,2}[\s_-]+\d{1,2}[-_:.]\d{1,2}([-_:.]\d{1,2})?\.png$",
            re.IGNORECASE,
        ),
        "screenshot",
    ),
    (
        "Screenshot_NNN.png",
        re.compile(r"^(screen[\s_-]?shot|screenshot)[_-]?\d{2,8}\.png$", re.IGNORECASE),
        "screenshot",
    ),
    (
        "CV / Resume",
        re.compile(r"^(cv|resume|curriculum[_-]?vitae)([_-][\w.-]*)?\.(pdf|docx|doc)$", re.IGNORECASE),
        "cv-resume",
    ),
]


def _candidate_filenames(ctx: UnitContext) -> list[str]:
    """The set of filenames this rule should match against."""
    if ctx.kind == "file":
        return [ctx.path.name]
    # Folder: use sample paths, fall back to siblings if no sample.
    if ctx.sample_paths:
        return [Path(p).name for p in ctx.sample_paths]
    return list(ctx.siblings)


def _match_patterns(filenames: list[str]) -> list[tuple[str, str, str]]:
    """Return list of (filename, pattern_name, class_id) for each match."""
    hits: list[tuple[str, str, str]] = []
    for name in filenames:
        for pattern_name, regex, class_id in PATTERNS:
            if regex.match(name):
                hits.append((name, pattern_name, class_id))
                break  # one pattern per filename
    return hits


class FilenamePatternRule(Rule):
    name = "by_filename_pattern"

    def applies(self, ctx: UnitContext) -> bool:
        return bool(_candidate_filenames(ctx))

    def evaluate(self, ctx: UnitContext) -> Classification | None:
        filenames = _candidate_filenames(ctx)
        hits = _match_patterns(filenames)
        if not hits:
            return None

        # File unit — single match, full confidence.
        if ctx.kind == "file":
            name, pattern_name, class_id = hits[0]
            return Classification(
                rule_name=self.name,
                confidence=0.95,
                class_id=class_id,
                evidence=[(name, f"filename matches pattern {pattern_name!r}")],
            )

        # Folder unit — only fire when one class dominates the sample.
        # We need >=85% of the sampled filenames to match the same class.
        class_counts = Counter(class_id for _, _, class_id in hits)
        dominant_class, dominant_count = class_counts.most_common(1)[0]
        share = dominant_count / max(len(filenames), 1)
        if share < 0.85:
            return None

        # Confidence scales with the share — full match -> 0.95, threshold -> 0.85.
        confidence = 0.85 + 0.10 * (share - 0.85) / 0.15
        confidence = min(0.95, max(0.85, confidence))

        # Map the dominant *file* class to a *folder* class where it makes sense.
        # A folder of `screenshot`-class files is a `screenshot-folder`. Other
        # classes don't have a folder-level alias yet, so we keep the file class.
        folder_class = "screenshot-folder" if dominant_class == "screenshot" else dominant_class

        # Cite up to three concrete matches for transparency.
        evidence: list[tuple[str, str]] = [
            (
                "filename-distribution",
                f"{share:.0%} of {len(filenames)} sample filenames match the {dominant_class!r} pattern family",
            ),
        ]
        for name, pattern_name, class_id in hits[:3]:
            if class_id == dominant_class:
                evidence.append((name, f"matches pattern {pattern_name!r}"))

        return Classification(
            rule_name=self.name,
            confidence=confidence,
            class_id=folder_class,
            evidence=evidence,
        )

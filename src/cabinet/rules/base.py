"""Rule plugin contract.

A Rule is a deterministic function: given a UnitContext (aggregated metadata
plus a bounded sample of file content), decide whether the unit belongs to
one of the classes in `config/content_classes.yaml`. Rules are cheap, return
fast, and never call out over the network. The LLM cascade in
`cabinet.classifier` is what runs when no rule reaches the confidence
threshold.

Every Classification carries the evidence that justifies it. This is
load-bearing — see CLAUDE.md hard rule #3 (citations required). A rule that
returns a class without populating `evidence` is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UnitContext:
    """Everything a rule sees about a single classification target.

    Folder units carry aggregated metadata (extensions histogram, file count,
    mtime range) plus a small sample of representative files. File units are
    treated as a degenerate folder of size 1.
    """

    path: Path
    kind: str  # "folder" | "file"
    extensions: dict[str, int]  # ext -> count; only meaningful for folders
    file_count: int
    total_size: int  # bytes
    date_range: tuple[float, float] | None  # (min_mtime, max_mtime) as POSIX timestamps
    sample_paths: list[Path] = field(default_factory=list)
    sample_contents: dict[Path, bytes] = field(default_factory=dict)  # bounded; truncated upstream
    sample_exif: dict[Path, dict] = field(default_factory=dict)  # parsed EXIF for image samples
    siblings: list[str] = field(default_factory=list)  # names of parent folder's other entries
    parent_name: str = ""
    # Optional escape hatch for cross-unit rules (e.g. hash dedup needs the worklist).
    # Rules that don't need it ignore it. Kept as Any to avoid coupling base.py
    # to a specific worklist implementation.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    """A rule's verdict on a unit.

    `confidence` is in [0.0, 1.0]. The classifier cascade short-circuits on
    >=0.9 and escalates to LLM when the best rule scored <0.7.

    `evidence` is a list of (source, reason) pairs. Source is usually a path
    or a metadata key ("extension-histogram", str(path), "exif:GPSInfo").
    Reason is a one-liner explaining what about that source pointed at this
    class. The triage layer renders these verbatim.
    """

    rule_name: str
    confidence: float
    class_id: str
    evidence: list[tuple[str, str]]


class Rule:
    """Base class for deterministic classification rules.

    Subclasses set `name` and implement `applies` + `evaluate`. The
    discovery walker in `rules/__init__.py` instantiates one of each.
    """

    name: str = ""

    def applies(self, ctx: UnitContext) -> bool:
        """Cheap pre-check — does this rule even want to look at the unit?

        Default is True; override to skip work (e.g. EXIF rule skips
        non-image folders). Keep this side-effect-free.
        """
        return True

    def evaluate(self, ctx: UnitContext) -> Classification | None:
        """Produce a classification, or None if this rule has no opinion.

        Returning None is the right move when a rule looked but didn't see
        what it was looking for — don't fabricate a low-confidence
        classification just to have something. The cascade handles "no rule
        fired" by escalating to LLM.
        """
        raise NotImplementedError

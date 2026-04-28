"""Per-OS path conventions.

Cabinet writes archives + a review pile under the user's home directory by
default. Defaults are the same across platforms so paths stay predictable;
detection exists for downstream code that needs to know.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Default destinations — keep these stable; downstream tests rely on them.
DEFAULT_ARCHIVE_DIR_NAME: str = "cabinet-archive"
DEFAULT_REVIEW_PILE_DIR_NAME: str = "cabinet-review-pile"
DEFAULT_SYSTEM_DIR_NAME: str = "cabinet"


def detect() -> str:
    """Return one of ``"linux"``, ``"darwin"``, ``"windows"``.

    Anything else falls through to ``"linux"`` — cabinet is POSIX-shaped, and a
    surprising platform shouldn't crash the CLI before the user can override.
    """
    p = sys.platform.lower()
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win"):
        return "windows"
    return "linux"


def default_archive_root() -> Path:
    """Default archive destination — ``~/cabinet-archive``."""
    return (Path.home() / DEFAULT_ARCHIVE_DIR_NAME).resolve()


def default_review_pile() -> Path:
    """Default review pile destination — ``~/cabinet-review-pile``.

    The review pile is where 'trash' goes. Cabinet never unlinks; it relocates
    to a single user-visible folder the user can spot-check before manually
    emptying.
    """
    return (Path.home() / DEFAULT_REVIEW_PILE_DIR_NAME).resolve()


def default_system_dir() -> Path:
    """Default system dir — ``~/cabinet`` — for triage.md, plans, ledgers."""
    return (Path.home() / DEFAULT_SYSTEM_DIR_NAME).resolve()

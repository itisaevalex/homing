"""Per-OS path conventions for attic.

attic keeps all of its state under a single system dir (``~/attic`` by default):
config.toml, ledgers/, plans/, tombstones/, staging/. Defaults are stable across
platforms so paths stay predictable; ``detect`` exists for downstream code that
needs to branch on the OS.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_SYSTEM_DIR_NAME: str = "attic"


def detect() -> str:
    """Return one of ``"linux"``, ``"darwin"``, ``"windows"``.

    Anything else falls through to ``"linux"`` — attic is POSIX-shaped, and a
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


def default_system_dir() -> Path:
    """Default system dir — ``~/attic`` — for config, ledgers, plans, etc."""
    return (Path.home() / DEFAULT_SYSTEM_DIR_NAME).resolve()


def config_path(system_dir: Path) -> Path:
    """Path to ``config.toml`` under a system dir."""
    return system_dir / "config.toml"


def ledgers_dir(system_dir: Path) -> Path:
    return system_dir / "ledgers"


def plans_dir(system_dir: Path) -> Path:
    return system_dir / "plans"


def tombstones_dir(system_dir: Path) -> Path:
    return system_dir / "tombstones"


def staging_dir(system_dir: Path) -> Path:
    return system_dir / "staging"


def ensure_layout(system_dir: Path) -> None:
    """Create the full attic state-dir layout (idempotent)."""
    for d in (
        system_dir,
        ledgers_dir(system_dir),
        plans_dir(system_dir),
        tombstones_dir(system_dir),
        staging_dir(system_dir),
    ):
        d.mkdir(parents=True, exist_ok=True)

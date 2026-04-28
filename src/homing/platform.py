"""Platform detection and per-platform config loading.

The tool ships per-platform YAML in ``config/platforms/<name>.yaml`` at the
repo root. This module locates the repo root by walking up from this file
until it finds ``pyproject.toml``, then reads the requested config.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

_SUPPORTED_PLATFORMS = ("linux", "darwin", "windows")


def detect() -> str:
    """Return the canonical platform name for the current process.

    Returns one of ``"linux"``, ``"darwin"``, or ``"windows"``. Falls back to
    ``sys.platform`` verbatim for any platform we do not recognize so callers
    can produce a clear error message instead of silently picking the wrong
    config.
    """
    sp = sys.platform
    if sp.startswith("linux"):
        return "linux"
    if sp == "darwin":
        return "darwin"
    if sp in ("win32", "cygwin"):
        return "windows"
    return sp


def repo_root() -> Path:
    """Resolve the repo root by walking up until we find ``pyproject.toml``.

    Raises ``RuntimeError`` if no ancestor contains a ``pyproject.toml``.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        f"Could not locate repo root (no pyproject.toml in any ancestor of {here})"
    )


def config_path(platform_name: str) -> Path:
    """Return the absolute path to the YAML config for ``platform_name``."""
    return repo_root() / "config" / "platforms" / f"{platform_name}.yaml"


def load_config(platform_name: str | None = None) -> dict[str, Any]:
    """Load the per-platform YAML config.

    Args:
        platform_name: Override the detected platform. Defaults to ``detect()``.

    Returns:
        The parsed YAML as a dict. An empty file returns an empty dict.

    Raises:
        FileNotFoundError: When no config file exists for the requested platform.
        ValueError: When the YAML parses to something that is not a mapping.
    """
    name = platform_name or detect()
    path = config_path(name)
    if not path.is_file():
        supported = ", ".join(_SUPPORTED_PLATFORMS)
        raise FileNotFoundError(
            f"No platform config for '{name}' at {path}. "
            f"Supported platforms (with shipped config): {supported}."
        )
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"Platform config at {path} must be a YAML mapping at the top level, "
            f"got {type(loaded).__name__}."
        )
    return loaded

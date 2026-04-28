"""Rule plugin registry — discovers every :class:`Rule` subclass at import.

Drop a new file under ``src/homing/rules/`` that defines one or more
``Rule`` subclasses; the registry picks it up automatically the next
time :func:`all_rules` is called. There is no manual registration list.

The registry is built lazily and cached on the module. Tests that drop
fresh rule modules into the package can call :func:`_reset_registry` to
force a re-scan, but normal callers should not need to.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import Rule, RuleFinding, UnitSummary

__all__ = ["Rule", "RuleFinding", "UnitSummary", "all_rules"]

_registry_cache: list[type[Rule]] | None = None

# Files that are infrastructure, not rules.
_SKIP_MODULES = frozenset({"__init__", "base"})


def all_rules() -> list[type[Rule]]:
    """Return every registered :class:`Rule` subclass.

    Walks the ``homing.rules`` package directory once and imports each
    ``.py`` file (other than ``__init__.py`` and ``base.py``). Every
    direct or indirect subclass of :class:`Rule` becomes part of the
    registry. Order is stable: sorted by class name.
    """
    global _registry_cache
    if _registry_cache is not None:
        return list(_registry_cache)

    package_dir = Path(__file__).resolve().parent
    package_name = __name__

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.name in _SKIP_MODULES:
            continue
        importlib.import_module(f"{package_name}.{module_info.name}")

    found: set[type[Rule]] = set()
    _collect_subclasses(Rule, found)
    _registry_cache = sorted(found, key=lambda cls: cls.__name__)
    return list(_registry_cache)


def _collect_subclasses(cls: type[Rule], out: set[type[Rule]]) -> None:
    for sub in cls.__subclasses__():
        out.add(sub)
        _collect_subclasses(sub, out)


def _reset_registry() -> None:
    """Test hook: clear the cached registry so the next call re-scans."""
    global _registry_cache
    _registry_cache = None

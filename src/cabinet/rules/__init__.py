"""Rule plugin registry.

Walks this package directory at import time and collects every concrete
`Rule` subclass it finds. Adding a new rule means dropping a new file
alongside the existing ones — no registration boilerplate.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import Classification, Rule, UnitContext

__all__ = ["Classification", "Rule", "UnitContext", "all_rules"]

# Module-level cache. Discovery is cheap, but we only want to do it once.
_DISCOVERED: list[type[Rule]] | None = None


def _discover_rules() -> list[type[Rule]]:
    """Import every sibling module and return the Rule subclasses defined.

    We deliberately import by name (not a glob) so import errors surface
    loudly — a broken rule shouldn't silently disappear from the cascade.
    """
    pkg_path = Path(__file__).parent
    found: list[type[Rule]] = []

    for module_info in pkgutil.iter_modules([str(pkg_path)]):
        # Skip the base module — it defines the abstract Rule itself.
        if module_info.name in {"base", "__init__"}:
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, Rule)
                and attr is not Rule
                # Avoid re-collecting Rule subclasses re-exported from another module.
                and attr.__module__ == module.__name__
            ):
                found.append(attr)

    # Stable order for deterministic tie-breaks in the classifier cascade.
    found.sort(key=lambda cls: (cls.__module__, cls.__name__))
    return found


def all_rules() -> list[type[Rule]]:
    """Return the registered rule classes (cached)."""
    global _DISCOVERED
    if _DISCOVERED is None:
        _DISCOVERED = _discover_rules()
    return list(_DISCOVERED)

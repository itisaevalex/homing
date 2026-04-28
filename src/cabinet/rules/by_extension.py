"""Extension-distribution rule.

Cheapest signal we have. If a folder is overwhelmingly one extension family,
that often pins it down: a folder of .png with parent name "screenshots" is
a screenshot dump; a folder of .zip files is an archive dump; a tree mixing
.so/.dll/license/config files is vendored tooling.

This rule never looks at file content. It works off the extension histogram
already computed by Phase A enumeration.
"""

from __future__ import annotations

import re

from .base import Classification, Rule, UnitContext

# What "screenshot folder" looks like.
SCREENSHOT_PARENT_RE = re.compile(r"screenshot|screen[\s_-]?shot|screen[\s_-]?capture", re.IGNORECASE)
SCREENSHOT_DOMINANT_THRESHOLD = 0.85

# Archive bundles. ".tar.gz" appears as ".gz" in a single-extension histogram,
# but we also accept ".tgz" and the obvious archives.
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".bz2", ".xz"}
ARCHIVE_DOMINANT_THRESHOLD = 0.85

# Vendored-tooling parent names — node_modules, virtualenvs, etc.
VENDORED_PARENT_NAMES = {
    "node_modules",
    "venv",
    ".venv",
    "vendor",
    "__pycache__",
    "site-packages",
    "bower_components",
}

# Extensions that, mixed in volume, suggest a vendored tree (binaries +
# configs + license files). Tuned conservatively — vendored detection
# escalates to LLM unless the parent name is a giveaway.
VENDORED_EXTS = {".so", ".dll", ".dylib", ".a", ".lib", ".pyc", ".class", ".jar", ".node"}


def _dominant_share(extensions: dict[str, int], target_exts: set[str]) -> float:
    total = sum(extensions.values())
    if total == 0:
        return 0.0
    matched = sum(count for ext, count in extensions.items() if ext.lower() in target_exts)
    return matched / total


def _format_histogram_evidence(extensions: dict[str, int], top: int = 5) -> str:
    items = sorted(extensions.items(), key=lambda kv: -kv[1])[:top]
    return ", ".join(f"{ext or '<no-ext>'}={count}" for ext, count in items)


class ExtensionRule(Rule):
    name = "by_extension"

    def applies(self, ctx: UnitContext) -> bool:
        # Folders only — for a single file, by_filename_pattern carries more signal.
        return ctx.kind == "folder" and bool(ctx.extensions)

    def evaluate(self, ctx: UnitContext) -> Classification | None:
        ext_lower = {ext.lower(): count for ext, count in ctx.extensions.items()}
        png_share = ext_lower.get(".png", 0) / max(sum(ext_lower.values()), 1)
        archive_share = _dominant_share(ext_lower, ARCHIVE_EXTS)
        histogram = _format_histogram_evidence(ext_lower)

        # Screenshot folder: dominant .png AND a parent name that screams it.
        if png_share >= SCREENSHOT_DOMINANT_THRESHOLD and SCREENSHOT_PARENT_RE.search(ctx.parent_name or ""):
            return Classification(
                rule_name=self.name,
                confidence=0.95,
                class_id="screenshot-folder",
                evidence=[
                    (
                        "extension-histogram",
                        f"{png_share:.0%} .png ({sum(ext_lower.values())} files); top: {histogram}",
                    ),
                    ("parent-name", f"parent folder name {ctx.parent_name!r} matches /screenshot/i"),
                ],
            )

        # Archive bundle dump.
        if archive_share >= ARCHIVE_DOMINANT_THRESHOLD:
            return Classification(
                rule_name=self.name,
                confidence=0.92,
                class_id="archive-zip",
                evidence=[
                    (
                        "extension-histogram",
                        f"{archive_share:.0%} archive extensions; top: {histogram}",
                    ),
                ],
            )

        # Vendored tooling — strong signal when parent name is a giveaway.
        # We deliberately don't try to detect "mixed binaries/configs" via
        # pure extension counts: that misclassifies legitimate project trees.
        # The parent-name check is the discriminator.
        if (ctx.parent_name or "").lower() in VENDORED_PARENT_NAMES:
            return Classification(
                rule_name=self.name,
                confidence=0.93,
                class_id="vendored-tooling",
                evidence=[
                    ("parent-name", f"parent folder name {ctx.parent_name!r} is a known vendored-tooling root"),
                    ("extension-histogram", f"top: {histogram}"),
                ],
            )

        # Secondary: if VENDORED_EXTS dominate AND file count is high, escalate.
        # Confidence is intentionally below the cascade short-circuit — the
        # LLM should sanity-check before we label something "vendored".
        vendored_share = _dominant_share(ext_lower, VENDORED_EXTS)
        if vendored_share >= 0.5 and ctx.file_count >= 50:
            return Classification(
                rule_name=self.name,
                confidence=0.6,
                class_id="vendored-tooling",
                evidence=[
                    (
                        "extension-histogram",
                        f"{vendored_share:.0%} compiled/binary extensions over {ctx.file_count} files; top: {histogram}",
                    ),
                ],
            )

        return None

"""Hash-based duplicate detection.

This rule is structurally different from the others. The other rules look at
a single unit in isolation; dedup needs to know about every other unit in
the worklist to find identical content_hashes.

Contract
- The classifier passes the worklist in via `ctx.extra["worklist"]` as a
  mapping `path -> {"content_hash": str | None, "size": int, ...}` for files
  the worklist has already hashed (Phase A's job).
- Per CLAUDE.md hard rule #1, "duplicate" means *identified*, not deleted.
  We emit a finding the triage layer presents to the user; the user picks
  which copy is primary.
- Confidence is 1.0 — content hashes are exact equality. Cite both paths.
"""

from __future__ import annotations

from .base import Classification, Rule, UnitContext


class HashDedupRule(Rule):
    name = "by_hash_dedup"

    def applies(self, ctx: UnitContext) -> bool:
        # File-level only. Folders don't have a single content hash.
        if ctx.kind != "file":
            return False
        worklist = ctx.extra.get("worklist") if ctx.extra else None
        if not isinstance(worklist, dict) or not worklist:
            return False
        # Need this file's own hash to compare.
        own = worklist.get(str(ctx.path)) or worklist.get(ctx.path)
        if not own:
            return False
        return bool(own.get("content_hash"))

    def evaluate(self, ctx: UnitContext) -> Classification | None:
        worklist: dict = ctx.extra["worklist"]

        # Look up our own hash. Accept either str or Path keys.
        own_entry = worklist.get(str(ctx.path)) or worklist.get(ctx.path)
        if not own_entry:
            return None
        own_hash = own_entry.get("content_hash")
        if not own_hash:
            return None

        # Find other paths in the worklist with the same hash.
        # Skip ourselves; normalise paths to str for comparison.
        own_path_str = str(ctx.path)
        twins: list[str] = []
        for other_path, other_entry in worklist.items():
            if not isinstance(other_entry, dict):
                continue
            if other_entry.get("content_hash") != own_hash:
                continue
            other_str = str(other_path)
            if other_str == own_path_str:
                continue
            twins.append(other_str)

        if not twins:
            return None

        # We don't reclassify the file's *content type* — the dedup signal
        # is orthogonal to "what is this thing". We emit `unknown` as the
        # class with full confidence on the duplicate finding; the
        # classifier merges this with whatever the content rule says, and
        # the planner uses the dedup evidence to surface a dedupe action.
        evidence: list[tuple[str, str]] = [
            (own_path_str, f"content_hash={own_hash}"),
        ]
        for twin in sorted(twins):
            evidence.append((twin, f"identical content_hash={own_hash}"))

        return Classification(
            rule_name=self.name,
            confidence=1.0,
            class_id="duplicate",
            evidence=evidence,
        )

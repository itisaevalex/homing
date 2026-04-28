"""Phase B classifier — deterministic rule cascade then LLM fallback.

Order of operations (per CLAUDE.md hard rule #5):
  1. Run every applicable Rule. Pick the highest-confidence verdict.
  2. If best confidence >= 0.9 → return immediately. No LLM call.
  3. If best confidence in [0.7, 0.9) → return rule verdict, still no LLM.
  4. Otherwise (no rule fired or best < 0.7) → escalate to LLM.
       a. Text-only first: send metadata + text excerpts + taxonomy.
       b. If text response confidence < threshold AND vision allowed AND
          unit has images/PDFs in the sample → send a multimodal call.

The LLM uses tool-use with a `submit_classification` tool that pins the
output schema. The taxonomy + system prompt go in a cache_control block so
repeated calls in a single run hit the prompt cache.

This module never makes a real API call without a client — if the cascade
has no high-confidence answer and no `anthropic_client` was passed, it
raises a clear error pointing at ANTHROPIC_API_KEY. That's the contract
the tests rely on.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .rules import Classification, UnitContext, all_rules

# Confidence thresholds for the cascade.
RULE_SHORT_CIRCUIT = 0.9  # >= this skips LLM entirely
RULE_ACCEPT = 0.7  # >= this returns the rule verdict without LLM
LLM_VISION_ESCALATE = 0.7  # text LLM below this triggers vision (when allowed)

# How much of each sampled file's bytes the text-only LLM call sees, per file.
TEXT_EXCERPT_BYTES = 2_048
# Cap on how many sample files we send in any one LLM call.
MAX_SAMPLES_IN_PROMPT = 5

# Anthropic models. Sonnet for the classification call, deliberately not Opus —
# this is a per-unit task and we run it many times.
LLM_MODEL = "claude-sonnet-4-5"
LLM_MAX_TOKENS = 1_024


# Resolve the taxonomy path relative to the repo, but allow tests / callers
# to override via env var.
def _default_taxonomy_path() -> Path:
    override = os.environ.get("CABINET_TAXONOMY_PATH")
    if override:
        return Path(override)
    # repo-root/config/content_classes.yaml — file at src/cabinet/classifier.py
    return Path(__file__).resolve().parents[2] / "config" / "content_classes.yaml"


@dataclass(frozen=True)
class _Taxonomy:
    raw_yaml: str
    class_ids: tuple[str, ...]


def _load_taxonomy(path: Path | None = None) -> _Taxonomy:
    p = path or _default_taxonomy_path()
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    classes = data.get("classes", []) or []
    ids = tuple(c["id"] for c in classes if isinstance(c, dict) and "id" in c)
    return _Taxonomy(raw_yaml=text, class_ids=ids)


# ---------------------------------------------------------------------------
# Rule cascade
# ---------------------------------------------------------------------------


def _run_rules(ctx: UnitContext) -> list[Classification]:
    """Run every applicable rule, return their verdicts in registration order."""
    verdicts: list[Classification] = []
    for rule_cls in all_rules():
        rule = rule_cls()
        try:
            if not rule.applies(ctx):
                continue
            verdict = rule.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 — defensive; one bad rule mustn't kill the cascade
            verdicts.append(
                Classification(
                    rule_name=rule.name or rule_cls.__name__,
                    confidence=0.0,
                    class_id="unknown",
                    evidence=[("rule-error", f"{type(exc).__name__}: {exc}")],
                )
            )
            continue
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts


def _pick_best(verdicts: list[Classification]) -> Classification | None:
    """Highest confidence wins; ties broken by registration order (stable)."""
    if not verdicts:
        return None
    return max(verdicts, key=lambda v: v.confidence)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


_SUBMIT_TOOL = {
    "name": "submit_classification",
    "description": (
        "Submit a single classification for the unit. You must pick a class_id "
        "from the taxonomy provided in the system prompt. Confidence is in "
        "[0, 1]. Evidence is a list of (source, reason) pairs — source is "
        "usually a path or metadata key, reason is one sentence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_id": {"type": "string", "description": "One of the class ids from the taxonomy."},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["source", "reason"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "rationale": {
                "type": "string",
                "description": "Optional brief reasoning, kept for the audit log.",
            },
        },
        "required": ["class_id", "confidence", "evidence"],
        "additionalProperties": False,
    },
}


_SYSTEM_PROMPT_HEAD = (
    "You are cabinet's classification component. You receive metadata and a "
    "small content sample for a single unit (a folder or file from a user's "
    "personal-document mountain) and must pick the best class_id from the "
    "taxonomy below.\n\n"
    "Hard rules:\n"
    "  1. class_id MUST be one of the ids in the taxonomy. If nothing fits, "
    "use 'unknown'.\n"
    "  2. Every evidence entry must point at a real source (a path, an "
    "EXIF key, an extension histogram). Never fabricate evidence.\n"
    "  3. Confidence reflects how sure you are. Below 0.6 means you are "
    "guessing — prefer 'unknown' or 'review' classes in that case.\n"
    "  4. Sensitive classes (passport, tax, contracts) require explicit "
    "evidence — don't guess them on weak signals.\n"
    "  5. Always call the submit_classification tool. Do not respond with "
    "free text.\n\n"
    "Taxonomy (YAML):\n"
)


def _build_system_prompt(taxonomy: _Taxonomy) -> list[dict[str, Any]]:
    """System prompt as a single cache-controlled block.

    Returning a list (instead of a plain string) lets us flag the block with
    cache_control so repeated calls in the same run reuse the prefix. The
    taxonomy is the bulk of the prompt, so this saves real money.
    """
    return [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT_HEAD + taxonomy.raw_yaml,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _excerpt(b: bytes, limit: int = TEXT_EXCERPT_BYTES) -> str:
    """Best-effort UTF-8 excerpt of a sampled file's first bytes."""
    if not b:
        return ""
    chunk = b[:limit]
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = chunk.decode("latin-1", errors="replace")
    return text


def _build_text_user_message(ctx: UnitContext) -> str:
    """Render the unit as a markdown blob the model can reason about."""
    lines: list[str] = []
    lines.append(f"# Unit: {ctx.path}")
    lines.append(f"- kind: {ctx.kind}")
    lines.append(f"- file_count: {ctx.file_count}")
    lines.append(f"- total_size_bytes: {ctx.total_size}")
    if ctx.parent_name:
        lines.append(f"- parent_name: {ctx.parent_name}")
    if ctx.date_range is not None:
        lines.append(f"- mtime_range: {ctx.date_range[0]:.0f}..{ctx.date_range[1]:.0f}")
    if ctx.extensions:
        top = sorted(ctx.extensions.items(), key=lambda kv: -kv[1])[:8]
        ext_str = ", ".join(f"{e or '<no-ext>'}={c}" for e, c in top)
        lines.append(f"- extensions: {ext_str}")
    if ctx.siblings:
        lines.append(f"- siblings (first 10): {', '.join(ctx.siblings[:10])}")
    if ctx.sample_paths:
        lines.append("- sample_paths:")
        for sp in ctx.sample_paths[:MAX_SAMPLES_IN_PROMPT]:
            lines.append(f"    - {sp}")
    if ctx.sample_exif:
        lines.append("- sample_exif (parsed, may be partial):")
        for path, exif in list(ctx.sample_exif.items())[:MAX_SAMPLES_IN_PROMPT]:
            keys = ", ".join(sorted(exif.keys())) if isinstance(exif, dict) else str(type(exif))
            lines.append(f"    - {path}: {keys}")
    if ctx.sample_contents:
        lines.append("- sample_contents (first ~2KB each, utf-8 best-effort):")
        for path, blob in list(ctx.sample_contents.items())[:MAX_SAMPLES_IN_PROMPT]:
            excerpt = _excerpt(blob)
            lines.append(f"### {path}")
            lines.append("```")
            lines.append(excerpt)
            lines.append("```")
    lines.append(
        "\nClassify this unit. Call submit_classification with a class_id from the taxonomy."
    )
    return "\n".join(lines)


def _parse_tool_response(message: Any, taxonomy: _Taxonomy) -> Classification | None:
    """Pull the submit_classification tool call off the response."""
    blocks = getattr(message, "content", None) or []
    for block in blocks:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if block_type != "tool_use":
            continue
        name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        if name != "submit_classification":
            continue
        raw_input = getattr(block, "input", None) or (
            block.get("input") if isinstance(block, dict) else None
        )
        if isinstance(raw_input, str):
            try:
                raw_input = json.loads(raw_input)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw_input, dict):
            return None
        class_id = raw_input.get("class_id", "unknown")
        if taxonomy.class_ids and class_id not in taxonomy.class_ids:
            # Refuse off-taxonomy outputs — fall back to unknown with a note.
            evidence = [
                ("llm:invalid-class", f"model returned non-taxonomy class_id={class_id!r}")
            ]
            return Classification(
                rule_name="llm",
                confidence=0.0,
                class_id="unknown",
                evidence=evidence,
            )
        confidence = float(raw_input.get("confidence", 0.0))
        evidence_raw = raw_input.get("evidence") or []
        evidence: list[tuple[str, str]] = []
        for item in evidence_raw:
            if isinstance(item, dict):
                src = str(item.get("source", ""))
                reason = str(item.get("reason", ""))
                if src or reason:
                    evidence.append((src, reason))
        if not evidence:
            evidence.append(("llm", "model provided no explicit evidence"))
        return Classification(
            rule_name="llm",
            confidence=confidence,
            class_id=class_id,
            evidence=evidence,
        )
    return None


def _call_llm_text(
    client: Any, ctx: UnitContext, taxonomy: _Taxonomy
) -> Classification | None:
    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        system=_build_system_prompt(taxonomy),
        tools=[_SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_classification"},
        messages=[{"role": "user", "content": _build_text_user_message(ctx)}],
    )
    return _parse_tool_response(response, taxonomy)


# Image / PDF media types we'll attempt to ship as multimodal blocks.
_VISION_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
_VISION_PDF_EXTS = {".pdf"}


def _vision_targets(ctx: UnitContext) -> list[Path]:
    """Pick up to 3 sample paths suitable for vision."""
    targets: list[Path] = []
    for p in ctx.sample_paths:
        suffix = Path(p).suffix.lower()
        if suffix in _VISION_IMAGE_EXTS or suffix in _VISION_PDF_EXTS:
            targets.append(Path(p))
        if len(targets) >= 3:
            break
    return targets


def _render_image_block(path: Path) -> dict[str, Any] | None:
    """Render a sample as a base64 image block.

    For images, we try to thumbnail to <=768px on the long edge to keep the
    payload small. For PDFs, we render page 1 via pdf2image. Both imports
    are lazy — if the deps are missing we skip the file instead of crashing.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in _VISION_PDF_EXTS:
            try:
                from pdf2image import convert_from_path  # type: ignore
            except Exception:
                return None
            pages = convert_from_path(str(path), first_page=1, last_page=1, dpi=120)
            if not pages:
                return None
            image = pages[0]
            media_type = "image/png"
        else:
            try:
                from PIL import Image  # type: ignore
            except Exception:
                return None
            image = Image.open(path)
            image.load()
            # Ext->media-type table; default to JPEG for safety.
            media_type = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/jpeg")

        # Thumbnail in-place to bound payload size.
        image.thumbnail((768, 768))

        from io import BytesIO

        buf = BytesIO()
        save_format = "PNG" if media_type == "image/png" else "JPEG"
        if save_format == "JPEG" and image.mode != "RGB":
            image = image.convert("RGB")
        image.save(buf, format=save_format)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        }
    except Exception:  # noqa: BLE001 — vision is best-effort by design
        return None


def _call_llm_vision(
    client: Any, ctx: UnitContext, taxonomy: _Taxonomy
) -> Classification | None:
    targets = _vision_targets(ctx)
    if not targets:
        return None

    content: list[dict[str, Any]] = []
    rendered = 0
    for path in targets:
        block = _render_image_block(path)
        if block is None:
            continue
        content.append({"type": "text", "text": f"Image from: {path}"})
        content.append(block)
        rendered += 1
    if rendered == 0:
        return None

    content.append({"type": "text", "text": _build_text_user_message(ctx)})

    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        system=_build_system_prompt(taxonomy),
        tools=[_SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_classification"},
        messages=[{"role": "user", "content": content}],
    )
    return _parse_tool_response(response, taxonomy)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_unit(
    ctx: UnitContext,
    *,
    anthropic_client: Any | None = None,
    allow_vision: bool = True,
    taxonomy_path: Path | None = None,
) -> Classification:
    """Classify a single unit. See module docstring for the cascade order.

    Raises:
        RuntimeError: if rules can't reach RULE_ACCEPT and no anthropic_client
            was passed. The error message points at ANTHROPIC_API_KEY so the
            caller knows the fix.
    """
    verdicts = _run_rules(ctx)
    best_rule = _pick_best(verdicts)
    if best_rule is not None and best_rule.confidence >= RULE_SHORT_CIRCUIT:
        return best_rule

    if best_rule is not None and best_rule.confidence >= RULE_ACCEPT:
        return best_rule

    # Need to escalate.
    if anthropic_client is None:
        rule_summary = (
            f" best rule verdict: {best_rule.class_id}@{best_rule.confidence:.2f} "
            f"({best_rule.rule_name})"
            if best_rule is not None
            else " no rule fired"
        )
        raise RuntimeError(
            "classify_unit: rule cascade insufficient and no anthropic_client provided."
            f"{rule_summary}. Pass anthropic.Anthropic() (set ANTHROPIC_API_KEY) "
            "or use a fixture client in tests."
        )

    taxonomy = _load_taxonomy(taxonomy_path)
    text_verdict = _call_llm_text(anthropic_client, ctx, taxonomy)

    # If text was confident enough, we're done.
    if text_verdict is not None and text_verdict.confidence >= LLM_VISION_ESCALATE:
        return text_verdict

    # Else escalate to vision IF allowed AND there's something to look at.
    if allow_vision and _vision_targets(ctx):
        vision_verdict = _call_llm_vision(anthropic_client, ctx, taxonomy)
        if vision_verdict is not None:
            return vision_verdict

    # Fall back to whatever we have. Prefer the LLM text verdict over the
    # rule guess when both exist; if neither fired meaningfully, mark unknown.
    if text_verdict is not None:
        return text_verdict
    if best_rule is not None:
        return best_rule
    return Classification(
        rule_name="cascade",
        confidence=0.0,
        class_id="unknown",
        evidence=[(str(ctx.path), "no rule fired and LLM returned no usable response")],
    )

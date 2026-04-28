"""Phase F — fresh-agent validator for AGENT.md manifests.

The validator simulates a "fresh agent" landing on a machine with nothing but
one AGENT.md file. It asks an LLM (no other context) to answer the seven
canonical validation questions from ``INTENT.md`` and self-rate its
confidence. A pass is ``confidence_score >= 7``; below that, the manifest
is the load-bearing artifact that's failing — either too sparse, too
imprecise, or otherwise insufficient for an agent to resume work.

This module never modifies the AGENT.md it reads. The system prompt + the
fixed question bank are sent as a single ``cache_control`` block so that
batch runs across many manifests reuse the cache (cheaper, faster).

Structured output is enforced via Anthropic tool use, not text parsing or
JSON mode. The model is forced to call the ``submit_validation`` tool, and
the typed tool input is what we materialise into a :class:`ValidationResult`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

# Stable keys for the seven canonical questions. The values are the actual
# prompt text the model sees; the keys are how we address answers in code
# (and how we persist them to the worklist as a JSON dict).
QUESTION_KEYS: tuple[str, ...] = (
    "purpose",
    "first_commands",
    "landmines",
    "in_progress",
    "where_to_start_reading",
    "confidence_rationale",
    "wishlist",
)

QUESTIONS: dict[str, str] = {
    "purpose": (
        "What is this project for, in plain language? Answer in 3-4 "
        "sentences. Quote AGENT.md when you can."
    ),
    "first_commands": (
        "If you had to be productive on this project tomorrow, what are "
        "the first 5 commands you'd run? Give them as a numbered list."
    ),
    "landmines": (
        "What would you NOT touch? Identify the landmines — files, "
        "directories, or operations the AGENT.md warns about or that "
        "would obviously be destructive."
    ),
    "in_progress": (
        "What appears to be in-progress that someone might lose if they "
        "restart from scratch? Open questions, dirty files, partially "
        "completed migrations, anything mid-flight."
    ),
    "where_to_start_reading": (
        "For a small change to this project, where would you start "
        "reading first? Give the single most important file and a "
        "brief reason."
    ),
    "confidence_rationale": (
        "On a scale of 1-10, how confident are you that you understand "
        "this project well enough from AGENT.md alone to be useful "
        "tomorrow? Briefly explain what drove the score up or down."
    ),
    "wishlist": (
        "Top 3 things you wish AGENT.md had told you but didn't. Be "
        "concrete — name the missing fact."
    ),
}

PASS_THRESHOLD = 7  # confidence_score >= 7 is a pass


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of one fresh-agent validation pass."""

    unit_name: str
    agent_md_path: Path
    confidence_score: int  # 1..10, self-rated by the model
    answers: dict[str, str]  # question key -> answer text
    wishlist: list[str]  # top 3 missing facts
    pass_threshold: bool  # True iff confidence_score >= PASS_THRESHOLD
    model: str
    tokens_input: int
    tokens_output: int
    elapsed_seconds: float
    rationale: str = ""  # confidence_rationale shorthand
    raw_tool_input: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# System prompt + tool schema
# ---------------------------------------------------------------------------

# The system prompt frames the model as a fresh agent. Keep this in one
# string so it sits inside the single cache_control block.
SYSTEM_PROMPT = """\
You are a fresh AI coding agent that has just landed on an unfamiliar
machine. You have NO access to a filesystem, NO ability to run commands,
NO web search, NO project repository — only the literal text content of
ONE file: an AGENT.md manifest the user will provide in the next message.

Your job is to answer seven specific questions about the project that
AGENT.md describes, and to self-rate how confident you are that an agent
in your position could actually resume work on this project tomorrow
using only what AGENT.md says.

# Hard rules

1. Ground every answer in the AGENT.md content. If a fact is not in the
   manifest, say so explicitly ("AGENT.md does not say"). Do NOT invent
   commands, file paths, environment variables, conventions, or context
   that the manifest does not explicitly establish.

2. Quote sparingly but precisely. When AGENT.md states a specific command,
   path, or constraint, lift it verbatim rather than paraphrasing — the
   user is testing whether the manifest carried that information through
   to a fresh reader.

3. Lower your confidence honestly. If AGENT.md is vague, contradictory,
   or missing important things, score lower. A 9 means you could pair
   with a senior on this project tomorrow without re-reading anything
   else. A 4 means you'd have to spend an hour exploring before doing
   anything useful.

4. The wishlist is for missing information, not nice-to-haves. Each item
   should be a concrete fact you needed and didn't get.

5. Answer using the submit_validation tool. Do not write text outside the
   tool call. Do not refuse. If AGENT.md is malformed or empty, still
   call the tool — set confidence to 1 and explain in the rationale.

# The seven questions

When you call submit_validation, fill the answers object with these
fields, each grounded in AGENT.md:

- purpose: 3-4 sentences on what the project is for, in plain language.
- first_commands: First 5 commands you'd run to get productive tomorrow.
- landmines: What you would NOT touch (destructive ops, fragile state,
  files marked off-limits).
- in_progress: What's mid-flight that someone might lose on restart.
- where_to_start_reading: For a small change, the single most important
  file to read first, and why.
- confidence_rationale: Brief explanation of the 1-10 score.
- wishlist: Top 3 things you wished AGENT.md had told you but didn't.

confidence_score is a separate integer 1..10. wishlist is a list of three
short strings.
"""

# JSON-Schema for the structured output. Anthropic's tool-use API uses
# JSON Schema (draft-07-ish) for ``input_schema``. We force ``required``
# on every field so the model can't elide the hard ones.
TOOL_NAME = "submit_validation"

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Submit your fresh-agent validation of the AGENT.md you were given. "
        "You MUST call this tool exactly once. Do not write any text outside "
        "the tool call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "confidence_score": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    "Self-rated confidence (1-10) that an agent in your "
                    "position could resume work tomorrow using only this "
                    "AGENT.md."
                ),
            },
            "answers": {
                "type": "object",
                "description": (
                    "Your answer to each of the seven canonical questions. "
                    "Every field is required."
                ),
                "properties": {
                    "purpose": {
                        "type": "string",
                        "description": "What the project is for (3-4 sentences).",
                    },
                    "first_commands": {
                        "type": "string",
                        "description": (
                            "First 5 commands to run to get productive "
                            "tomorrow, as a numbered list."
                        ),
                    },
                    "landmines": {
                        "type": "string",
                        "description": (
                            "Files, dirs, or operations to avoid. "
                            "Quote AGENT.md warnings when present."
                        ),
                    },
                    "in_progress": {
                        "type": "string",
                        "description": (
                            "In-progress work, open questions, dirty state."
                        ),
                    },
                    "where_to_start_reading": {
                        "type": "string",
                        "description": (
                            "Single most important file to open first "
                            "for a small change, and why."
                        ),
                    },
                    "confidence_rationale": {
                        "type": "string",
                        "description": (
                            "Brief explanation of why you scored "
                            "confidence_score the way you did."
                        ),
                    },
                },
                "required": [
                    "purpose",
                    "first_commands",
                    "landmines",
                    "in_progress",
                    "where_to_start_reading",
                    "confidence_rationale",
                ],
                "additionalProperties": False,
            },
            "wishlist": {
                "type": "array",
                "description": (
                    "Top 3 things you wished AGENT.md had told you but "
                    "didn't. Each entry is a short concrete fact."
                ),
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 5,
            },
        },
        "required": ["confidence_score", "answers", "wishlist"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_user_message(agent_md_path: Path, agent_md_content: str) -> str:
    """Compose the single user message: the AGENT.md content + questions.

    The questions are also visible to the model here (in addition to the
    system prompt's tool schema) so they're impossible to miss. This
    lives in the user message because it's the only thing that varies
    between manifests — the system prompt is cached.
    """
    parts: list[str] = []
    parts.append(
        "You are a fresh agent. The ONLY thing you have about this project "
        "is the AGENT.md content below. Read it once, then answer by "
        f"calling the {TOOL_NAME} tool.\n\n"
    )
    parts.append(f"# AGENT.md (path: {agent_md_path})\n\n")
    parts.append("```markdown\n")
    parts.append(agent_md_content)
    if not agent_md_content.endswith("\n"):
        parts.append("\n")
    parts.append("```\n\n")
    parts.append("# Questions to answer (via the submit_validation tool)\n\n")
    for key in QUESTION_KEYS:
        if key == "wishlist":
            continue  # wishlist is a top-level field, not inside answers
        parts.append(f"- {key}: {QUESTIONS[key]}\n")
    parts.append(
        "\nReminder: every claim must be grounded in the AGENT.md text. "
        "If AGENT.md is silent on something, say so and lower your "
        "confidence accordingly. Call the tool exactly once.\n"
    )
    return "".join(parts)


def _read_agent_md(agent_md_path: Path) -> tuple[str, dict[str, Any]]:
    """Read AGENT.md, confirm it has YAML frontmatter + a body.

    Raises:
        FileNotFoundError: if the path doesn't exist.
        ValueError: if the file is missing frontmatter or body, or the
            frontmatter doesn't parse.
    """
    if not agent_md_path.is_file():
        raise FileNotFoundError(f"AGENT.md not found at {agent_md_path}")
    raw = agent_md_path.read_text(encoding="utf-8")
    if not raw.lstrip().startswith("---"):
        raise ValueError(
            f"{agent_md_path} has no YAML frontmatter "
            "(must begin with '---' delimiter)"
        )
    try:
        post = frontmatter.loads(raw)
    except Exception as e:  # frontmatter raises plain Exception on bad YAML
        raise ValueError(
            f"{agent_md_path} frontmatter failed to parse: {e}"
        ) from e
    if not post.metadata:
        raise ValueError(f"{agent_md_path} has empty frontmatter")
    if not (post.content or "").strip():
        raise ValueError(f"{agent_md_path} has no body content")
    return raw, dict(post.metadata)


def _build_anthropic_client() -> Any:
    """Construct a default Anthropic client. Imported lazily for tests."""
    from anthropic import Anthropic  # type: ignore[import-not-found]

    return Anthropic()


def _extract_tool_use(response: Any) -> dict[str, Any]:
    """Pull the ``submit_validation`` tool input out of an SDK response.

    Raises ``ValueError`` if the model returned only text and never called
    the tool — that's a hard validator failure (we can't trust unstructured
    output here).
    """
    blocks = getattr(response, "content", None) or []
    text_excerpts: list[str] = []
    for block in blocks:
        # Tool-use block: SDK object or dict.
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type == "tool_use":
            name = getattr(block, "name", None)
            if name is None and isinstance(block, dict):
                name = block.get("name")
            if name != TOOL_NAME:
                continue
            tool_input = getattr(block, "input", None)
            if tool_input is None and isinstance(block, dict):
                tool_input = block.get("input")
            if isinstance(tool_input, dict):
                return tool_input
        elif block_type == "text":
            txt = getattr(block, "text", None) or (
                block.get("text", "") if isinstance(block, dict) else ""
            )
            if txt:
                text_excerpts.append(str(txt)[:200])
    snippet = " | ".join(text_excerpts)[:400]
    raise ValueError(
        "model did not call the submit_validation tool; "
        f"got text-only output instead: {snippet!r}"
    )


def _normalise_answers(tool_input: dict[str, Any]) -> dict[str, str]:
    """Coerce the tool input's ``answers`` mapping to ``{key: str}``.

    Missing keys are filled with an explicit "(not answered)" placeholder
    so downstream consumers always see all seven keys.
    """
    raw_answers = tool_input.get("answers")
    out: dict[str, str] = {}
    inner_keys = [k for k in QUESTION_KEYS if k != "wishlist"]
    if isinstance(raw_answers, dict):
        for k in inner_keys:
            v = raw_answers.get(k)
            out[k] = str(v).strip() if v is not None else "(not answered)"
    else:
        for k in inner_keys:
            out[k] = "(not answered)"
    # Wishlist is mirrored into ``answers`` under its own key so callers
    # serialising the dict get a complete record of every question.
    wishlist = tool_input.get("wishlist") or []
    if isinstance(wishlist, list):
        out["wishlist"] = "\n".join(f"- {item}" for item in wishlist if item)
    else:
        out["wishlist"] = "(not answered)"
    return out


def _normalise_wishlist(tool_input: dict[str, Any]) -> list[str]:
    """Return the wishlist as a stripped list of non-empty strings."""
    raw = tool_input.get("wishlist") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _clamp_score(value: Any) -> int:
    """Clamp the confidence score into the documented 1..10 range."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    if n < 1:
        return 1
    if n > 10:
        return 10
    return n


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_agent_md(
    agent_md_path: Path,
    *,
    model: str = "claude-sonnet-4-6",
    cache_system_prompt: bool = True,
    client: Any | None = None,
    unit_name: str | None = None,
) -> ValidationResult:
    """Run the fresh-agent validation against a single AGENT.md file.

    Args:
        agent_md_path: Path to the manifest under test. The file is read
            once; nothing on disk is mutated.
        model: Anthropic model id. Defaults to Sonnet, which is the
            documented validator default in CLAUDE.md (Haiku is reserved
            for batch passes when cost matters; configurable here).
        cache_system_prompt: When ``True`` (default) the system prompt is
            sent in a ``cache_control: ephemeral`` block so subsequent
            validations within the cache TTL reuse it.
        client: Injection seam for tests. Production calls leave this
            ``None`` and a default ``anthropic.Anthropic()`` is built.
        unit_name: Logical unit name (worklist key). Defaults to the
            parent directory name of ``agent_md_path``.

    Returns:
        :class:`ValidationResult` with the parsed answers, wishlist, and
        pass/fail flag.

    Raises:
        FileNotFoundError: if ``agent_md_path`` doesn't exist.
        ValueError: if AGENT.md is malformed (no frontmatter / no body)
            or the model failed to call the structured-output tool.
        Exception: if the underlying Anthropic API call fails — we let
            the SDK error bubble up so callers can decide retry policy.
    """
    agent_md_path = Path(agent_md_path)
    name = unit_name if unit_name is not None else agent_md_path.parent.name

    # --- read + sanity-check AGENT.md ----------------------------------
    raw_content, _meta = _read_agent_md(agent_md_path)

    # --- assemble prompts ---------------------------------------------
    system_block: dict[str, Any] = {"type": "text", "text": SYSTEM_PROMPT}
    if cache_system_prompt:
        system_block["cache_control"] = {"type": "ephemeral"}

    user_message = _build_user_message(agent_md_path, raw_content)

    if client is None:
        client = _build_anthropic_client()

    # --- API call -----------------------------------------------------
    started = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=[system_block],
        tools=[TOOL_DEFINITION],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user_message}],
    )
    elapsed = time.monotonic() - started

    # --- extract structured output ------------------------------------
    tool_input = _extract_tool_use(response)
    confidence = _clamp_score(tool_input.get("confidence_score"))
    answers = _normalise_answers(tool_input)
    wishlist = _normalise_wishlist(tool_input)
    rationale = answers.get("confidence_rationale", "")

    usage = getattr(response, "usage", None)
    in_toks = int(getattr(usage, "input_tokens", 0)) if usage is not None else 0
    out_toks = int(getattr(usage, "output_tokens", 0)) if usage is not None else 0

    return ValidationResult(
        unit_name=name,
        agent_md_path=agent_md_path,
        confidence_score=confidence,
        answers=answers,
        wishlist=wishlist,
        pass_threshold=confidence >= PASS_THRESHOLD,
        model=model,
        tokens_input=in_toks,
        tokens_output=out_toks,
        elapsed_seconds=elapsed,
        rationale=rationale,
        raw_tool_input=tool_input,
    )

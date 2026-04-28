"""Tests for ``homing.validate`` (Phase F).

The Anthropic client is fully mocked — these tests never touch the real
API. We exercise:

* the system prompt frames the model as a fresh agent with no context,
* the ``submit_validation`` tool schema is sent correctly,
* a well-formed tool_use response materialises into a ``ValidationResult``,
* the pass threshold is exactly 7 (6 fails, 7 passes),
* malformed AGENT.md (no frontmatter, no body) raises clear errors,
* an API error surfaces as an exception rather than a silent pass,
* a model that responds with text only (no tool call) raises ValueError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from homing import validate as validate_mod
from homing.validate import (
    PASS_THRESHOLD,
    QUESTION_KEYS,
    TOOL_NAME,
    ValidationResult,
    validate_agent_md,
)


# ---------------------------------------------------------------------------
# Fakes that look like the Anthropic SDK
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 500


@dataclass
class _FakeToolUseBlock:
    name: str
    input: dict[str, Any]
    type: str = "tool_use"
    id: str = "toolu_test_1"


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[Any]
    usage: _FakeUsage = field(default_factory=_FakeUsage)


def _good_tool_input(score: int = 8) -> dict[str, Any]:
    """Return a tool_use ``input`` that satisfies the schema."""
    return {
        "confidence_score": score,
        "answers": {
            "purpose": "It is a financial-filing scraper fleet covering six countries.",
            "first_commands": "1. cd runner\n2. pip install -r requirements.txt\n3. python runner.py status\n4. python runner.py run\n5. python runner.py dashboard",
            "landmines": "Do not regenerate filings_cache.db, documents/, or filings.json — those are crawled artifacts.",
            "in_progress": "Australia date format and table rename are open. enricher has no GitHub remote.",
            "where_to_start_reading": "SCRAPER_SPEC.md — it is the authoritative reference.",
            "confidence_rationale": "AGENT.md is rich and citation-driven; commands and landmines are explicit.",
        },
        "wishlist": [
            "the actual repo URL of the consumer pipeline",
            "an explicit `.env.example`",
            "test commands per sub-project",
        ],
    }


def _make_client(tool_input: dict[str, Any] | None = None) -> MagicMock:
    """Return a MagicMock shaped like ``anthropic.Anthropic()``."""
    client = MagicMock()
    if tool_input is None:
        tool_input = _good_tool_input()
    client.messages.create.return_value = _FakeResponse(
        content=[_FakeToolUseBlock(name=TOOL_NAME, input=tool_input)],
    )
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_VALID_AGENT_MD = """\
---
name: financialreports
purpose: A multi-country scraper fleet.
state: active
type: service
stack: [Python]
---

# Financial Filing Scraper Fleet

## What this is

A scraper fleet that collects filings from six exchanges.

## How to run

```bash
cd runner && python runner.py run
```

## Known issues

None today.
"""


_NO_FRONTMATTER = """\
# AGENT.md

This file has no YAML frontmatter.
"""


_EMPTY_BODY = """\
---
name: empty
purpose: Has frontmatter but no body.
---

"""


@pytest.fixture
def valid_agent_md(tmp_path: Path) -> Path:
    p = tmp_path / "alpha" / "AGENT.md"
    p.parent.mkdir(parents=True)
    p.write_text(_VALID_AGENT_MD, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_returns_result_for_well_formed_agent_md(
    valid_agent_md: Path,
) -> None:
    client = _make_client(_good_tool_input(score=8))
    result = validate_agent_md(valid_agent_md, client=client)

    assert isinstance(result, ValidationResult)
    assert result.unit_name == "alpha"
    assert result.agent_md_path == valid_agent_md
    assert result.confidence_score == 8
    assert result.pass_threshold is True
    assert result.tokens_input == 1000
    assert result.tokens_output == 500
    assert result.elapsed_seconds >= 0.0
    # Every question-key answer is materialised.
    for k in QUESTION_KEYS:
        assert k in result.answers
    # Wishlist comes through as a list of three strings.
    assert isinstance(result.wishlist, list)
    assert len(result.wishlist) == 3


def test_validate_constructs_request_with_fresh_agent_framing(
    valid_agent_md: Path,
) -> None:
    """The system prompt MUST tell the model it has no other context."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse(
            content=[_FakeToolUseBlock(name=TOOL_NAME, input=_good_tool_input())]
        )

    client = MagicMock()
    client.messages.create.side_effect = _capture

    validate_agent_md(valid_agent_md, client=client)

    # System prompt is a list with cache_control on the single block.
    sys_blocks = captured["system"]
    assert isinstance(sys_blocks, list) and len(sys_blocks) == 1
    sys_text = sys_blocks[0]["text"]
    # Normalise whitespace so word-wrapping doesn't fool the assertions.
    flat = " ".join(sys_text.lower().split())
    # Fresh-agent framing is present.
    assert "fresh" in flat
    assert "unfamiliar machine" in flat
    assert "no access" in flat
    # Anti-hallucination clause is present.
    assert "do not invent" in flat
    # Cache control is set.
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_validate_sends_tool_schema_and_forces_tool_use(
    valid_agent_md: Path,
) -> None:
    """The submit_validation tool must be defined and tool_choice forced."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse(
            content=[_FakeToolUseBlock(name=TOOL_NAME, input=_good_tool_input())]
        )

    client = MagicMock()
    client.messages.create.side_effect = _capture

    validate_agent_md(valid_agent_md, client=client)

    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    tool = tools[0]
    assert tool["name"] == TOOL_NAME
    schema = tool["input_schema"]
    # Required top-level fields are exactly what we contract on.
    assert set(schema["required"]) == {"confidence_score", "answers", "wishlist"}
    # confidence_score is bounded 1..10.
    cs = schema["properties"]["confidence_score"]
    assert cs["type"] == "integer"
    assert cs["minimum"] == 1
    assert cs["maximum"] == 10
    # tool_choice forces THIS tool, not "auto".
    tc = captured["tool_choice"]
    assert tc == {"type": "tool", "name": TOOL_NAME}


def test_validate_user_message_includes_agent_md_content(
    valid_agent_md: Path,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse(
            content=[_FakeToolUseBlock(name=TOOL_NAME, input=_good_tool_input())]
        )

    client = MagicMock()
    client.messages.create.side_effect = _capture

    validate_agent_md(valid_agent_md, client=client)

    user_msg = captured["messages"][0]["content"]
    assert "Financial Filing Scraper Fleet" in user_msg
    assert "name: financialreports" in user_msg
    # The questions block reaches the model too.
    assert "purpose:" in user_msg
    assert "first_commands:" in user_msg


# ---------------------------------------------------------------------------
# Threshold logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected_pass",
    [
        (1, False),
        (5, False),
        (6, False),  # 6 = fail (just below threshold)
        (7, True),  # 7 = pass (threshold)
        (8, True),
        (10, True),
    ],
)
def test_pass_threshold_is_exactly_seven(
    valid_agent_md: Path, score: int, expected_pass: bool
) -> None:
    client = _make_client(_good_tool_input(score=score))
    result = validate_agent_md(valid_agent_md, client=client)
    assert result.confidence_score == score
    assert result.pass_threshold is expected_pass
    assert PASS_THRESHOLD == 7


def test_score_is_clamped_into_one_through_ten(valid_agent_md: Path) -> None:
    too_high = _good_tool_input(score=8)
    too_high["confidence_score"] = 99
    client = _make_client(too_high)
    result = validate_agent_md(valid_agent_md, client=client)
    assert result.confidence_score == 10

    too_low = _good_tool_input(score=8)
    too_low["confidence_score"] = -3
    client = _make_client(too_low)
    result = validate_agent_md(valid_agent_md, client=client)
    assert result.confidence_score == 1


# ---------------------------------------------------------------------------
# Failure paths — malformed AGENT.md
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    client = _make_client()
    with pytest.raises(FileNotFoundError):
        validate_agent_md(tmp_path / "does-not-exist.md", client=client)
    # The model is never called.
    assert client.messages.create.call_count == 0


def test_no_frontmatter_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "bad" / "AGENT.md"
    p.parent.mkdir()
    p.write_text(_NO_FRONTMATTER, encoding="utf-8")
    client = _make_client()
    with pytest.raises(ValueError, match="frontmatter"):
        validate_agent_md(p, client=client)
    assert client.messages.create.call_count == 0


def test_empty_body_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "empty" / "AGENT.md"
    p.parent.mkdir()
    p.write_text(_EMPTY_BODY, encoding="utf-8")
    client = _make_client()
    with pytest.raises(ValueError, match="body"):
        validate_agent_md(p, client=client)


# ---------------------------------------------------------------------------
# Failure paths — API + model misbehaviour
# ---------------------------------------------------------------------------


def test_api_error_propagates(valid_agent_md: Path) -> None:
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom (rate limit)")
    with pytest.raises(RuntimeError, match="boom"):
        validate_agent_md(valid_agent_md, client=client)


def test_text_only_response_raises_value_error(valid_agent_md: Path) -> None:
    """If the model ignores tool_use and emits text, that's a hard fail."""
    client = MagicMock()
    client.messages.create.return_value = _FakeResponse(
        content=[_FakeTextBlock(text="I refuse to use the tool. Sorry.")],
    )
    with pytest.raises(ValueError, match="submit_validation"):
        validate_agent_md(valid_agent_md, client=client)


def test_wrong_tool_name_raises_value_error(valid_agent_md: Path) -> None:
    """A tool_use block with the wrong name is treated like no tool call."""
    client = MagicMock()
    client.messages.create.return_value = _FakeResponse(
        content=[_FakeToolUseBlock(name="something_else", input={})]
    )
    with pytest.raises(ValueError, match="submit_validation"):
        validate_agent_md(valid_agent_md, client=client)


def test_partial_answers_dict_filled_with_placeholder(valid_agent_md: Path) -> None:
    """Missing answer keys come back as '(not answered)' rather than KeyError."""
    sparse = {
        "confidence_score": 5,
        "answers": {"purpose": "tiny"},
        "wishlist": ["one"],
    }
    client = _make_client(sparse)
    result = validate_agent_md(valid_agent_md, client=client)
    assert result.answers["purpose"] == "tiny"
    assert result.answers["first_commands"] == "(not answered)"
    assert result.pass_threshold is False


# ---------------------------------------------------------------------------
# Cache toggle
# ---------------------------------------------------------------------------


def test_cache_system_prompt_can_be_disabled(valid_agent_md: Path) -> None:
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse(
            content=[_FakeToolUseBlock(name=TOOL_NAME, input=_good_tool_input())]
        )

    client = MagicMock()
    client.messages.create.side_effect = _capture

    validate_agent_md(
        valid_agent_md, client=client, cache_system_prompt=False
    )
    sys_block = captured["system"][0]
    assert "cache_control" not in sys_block


# ---------------------------------------------------------------------------
# Custom unit_name override
# ---------------------------------------------------------------------------


def test_explicit_unit_name_wins_over_directory_name(valid_agent_md: Path) -> None:
    client = _make_client()
    result = validate_agent_md(
        valid_agent_md, client=client, unit_name="custom-name"
    )
    assert result.unit_name == "custom-name"


# ---------------------------------------------------------------------------
# Module-level constants are wired correctly
# ---------------------------------------------------------------------------


def test_question_keys_match_module_questions() -> None:
    """The seven canonical question keys are stable and consistent."""
    assert len(QUESTION_KEYS) == 7
    # Every key is referenced in QUESTIONS or carried as a top-level field.
    inner = [k for k in QUESTION_KEYS if k != "wishlist"]
    for k in inner:
        assert k in validate_mod.QUESTIONS

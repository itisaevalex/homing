"""Tests for ``homing.draft``.

These tests never reach the real Anthropic API. The Anthropic client is
mocked, the schema file is replaced via monkeypatching, and a synthetic
project tree is built per test. The goal is to exercise:

* the no-silent-overwrite policy (CLAUDE.md hard rule #2),
* deterministic input collection ordering (so subsequent runs are stable),
* validation rejection on malformed LLM outputs,
* env-var name extraction (no values leaked).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from homing import draft as draft_mod
from homing.draft import (
    DraftResult,
    _extract_env_var_names,
    _validate_draft,
    collect_inputs,
    draft_agent_md,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 200


@dataclass
class _FakeBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    usage: _FakeUsage


def _make_fake_client(text: str) -> MagicMock:
    """Return a MagicMock shaped like ``anthropic.Anthropic()``."""
    client = MagicMock()
    client.messages.create.return_value = _FakeResponse(
        content=[_FakeBlock(text=text)],
        usage=_FakeUsage(),
    )
    return client


_VALID_DRAFT = """\
---
name: alpha
purpose: A toy project for tests.
state: active
type: tool
stack: [Python]
meta:
  sources: [README.md, pyproject.toml]
---

# Alpha

## What this is

Alpha is a toy project. It exists for the test bundle. (citation: README.md)

## How to run

```bash
pip install -e .
pytest
```

(citation: pyproject.toml)

## Agent instructions

- Read README.md first.
- Run tests with pytest.
- Do not modify generated files.

## Recent work

# TODO: no git history available in fixture.

## Known issues

# TODO: none recorded.
"""


_INVALID_DRAFT_NO_FRONTMATTER = """\
# Alpha

This is not a valid AGENT.md — it has no YAML frontmatter.
"""


_INVALID_DRAFT_MISSING_FIELDS = """\
---
name: alpha
purpose: A toy project.
---

# Alpha

## What this is

short.

## How to run

short.

## Agent instructions

short.
"""


@pytest.fixture
def schema_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a stub schema file and point the loader at it."""
    schema = tmp_path / "SCHEMA.md"
    schema.write_text("# AGENT.md schema (stub for tests)\n", encoding="utf-8")
    monkeypatch.setattr(draft_mod, "_SCHEMA_PATH_CANDIDATES", (schema,))
    return schema


@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    """Build a small synthetic project tree."""
    proj = tmp_path / "alpha"
    proj.mkdir()
    (proj / "README.md").write_text("# Alpha\n\nA toy project.\n", encoding="utf-8")
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (proj / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\nAgent contract for alpha.\n", encoding="utf-8"
    )
    (proj / ".env.example").write_text(
        "# Test env\nALPHA_API_KEY=please-set\nALPHA_DB_URL=postgres://user:pw@host/db\nNOT_AN_ENV_VAR\n",
        encoding="utf-8",
    )
    (proj / "src").mkdir()
    (proj / "src" / "alpha.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    return proj


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_accepts_well_formed_draft() -> None:
    outcome = _validate_draft(_VALID_DRAFT)
    assert outcome.ok, outcome.reason
    assert outcome.frontmatter is not None
    assert outcome.frontmatter["name"] == "alpha"


def test_validate_rejects_missing_frontmatter() -> None:
    outcome = _validate_draft(_INVALID_DRAFT_NO_FRONTMATTER)
    assert not outcome.ok
    assert outcome.reason is not None
    assert "frontmatter" in outcome.reason


def test_validate_rejects_missing_required_fields() -> None:
    outcome = _validate_draft(_INVALID_DRAFT_MISSING_FIELDS)
    assert not outcome.ok
    assert outcome.reason is not None
    assert "missing required fields" in outcome.reason


def test_validate_rejects_foreign_os_paths() -> None:
    bad = _VALID_DRAFT.replace(
        "Read README.md first.", "Read /Users/someone/foo/bar.md first."
    )
    outcome = _validate_draft(bad)
    assert not outcome.ok
    assert outcome.reason is not None
    assert "foreign-OS path" in outcome.reason


# ---------------------------------------------------------------------------
# .env.example name extraction (must NEVER leak values)
# ---------------------------------------------------------------------------


def test_env_extraction_returns_names_only() -> None:
    text = (
        "# comments are ignored\n"
        "API_KEY=abcdefg-secret\n"
        "DB_URL=postgres://u:p@h/d\n"
        "export ANOTHER=value\n"
        "lowercase=ignored\n"
        "\n"
    )
    names = _extract_env_var_names(text)
    assert names == ["API_KEY", "DB_URL", "ANOTHER"]
    # Critically: no values appear anywhere in the output.
    joined = "\n".join(names)
    assert "secret" not in joined
    assert "postgres" not in joined
    assert "value" not in joined


# ---------------------------------------------------------------------------
# Pre-processing — deterministic order
# ---------------------------------------------------------------------------


def test_collect_inputs_is_deterministic(fixture_project: Path) -> None:
    """Same project, two calls -> same labels in same order."""
    a = collect_inputs(fixture_project)
    b = collect_inputs(fixture_project)
    assert [(x.label, x.path) for x in a] == [(x.label, x.path) for x in b]


def test_collect_inputs_includes_expected_files(fixture_project: Path) -> None:
    items = collect_inputs(fixture_project)
    paths = [it.path for it in items]
    assert "(walk)" in paths  # top-level listing
    assert "CLAUDE.md" in paths
    assert "README.md" in paths
    assert "pyproject.toml" in paths
    assert ".env.example" in paths
    # And the env item must be the names-only flavour.
    env_item = next(it for it in items if it.path == ".env.example")
    assert "please-set" not in env_item.content
    assert "postgres" not in env_item.content
    assert "ALPHA_API_KEY" in env_item.content


def test_collect_inputs_respects_max_files(fixture_project: Path) -> None:
    items = collect_inputs(fixture_project, max_input_files=2)
    assert len(items) == 2


# ---------------------------------------------------------------------------
# Conflict policy (the load-bearing CLAUDE.md rule #2)
# ---------------------------------------------------------------------------


def test_proposed_policy_writes_sibling_when_target_exists(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    out_dir = tmp_path / "system" / "projects" / "alpha"
    out_dir.mkdir(parents=True)
    target = out_dir / "AGENT.md"
    sentinel = "# DO NOT TOUCH\n\nhuman-curated content.\n"
    target.write_text(sentinel, encoding="utf-8")

    client = _make_fake_client(_VALID_DRAFT)
    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="proposed",
        client=client,
    )

    assert result.status == "proposed"
    proposed = out_dir / "AGENT.proposed.md"
    assert proposed.exists()
    # Original is byte-identical to the sentinel.
    assert target.read_text(encoding="utf-8") == sentinel
    # Proposed has the LLM body.
    assert "name: alpha" in proposed.read_text(encoding="utf-8")


def test_skip_policy_does_not_call_llm(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    out_dir = tmp_path / "system" / "projects" / "alpha"
    out_dir.mkdir(parents=True)
    target = out_dir / "AGENT.md"
    target.write_text("existing\n", encoding="utf-8")

    client = _make_fake_client(_VALID_DRAFT)
    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="skip",
        client=client,
    )

    assert result.status == "skipped"
    assert client.messages.create.call_count == 0
    assert target.read_text(encoding="utf-8") == "existing\n"


def test_fail_policy_raises(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    out_dir = tmp_path / "system" / "projects" / "alpha"
    out_dir.mkdir(parents=True)
    target = out_dir / "AGENT.md"
    target.write_text("existing\n", encoding="utf-8")

    client = _make_fake_client(_VALID_DRAFT)
    with pytest.raises(FileExistsError):
        draft_agent_md(
            project_path=fixture_project,
            output_path=target,
            overwrite_policy="fail",
            client=client,
        )


def test_fresh_target_writes_directly(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    target = tmp_path / "system" / "projects" / "alpha" / "AGENT.md"
    client = _make_fake_client(_VALID_DRAFT)

    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="proposed",
        client=client,
    )

    assert result.status == "drafted"
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "name: alpha" in body


# ---------------------------------------------------------------------------
# LLM validation rejection
# ---------------------------------------------------------------------------


def test_invalid_llm_output_marks_failure_without_writing(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    target = tmp_path / "system" / "projects" / "alpha" / "AGENT.md"
    client = _make_fake_client(_INVALID_DRAFT_MISSING_FIELDS)

    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="proposed",
        client=client,
    )

    assert result.status == "failed"
    assert result.reason is not None
    assert "validation failed" in result.reason
    # No file should be written when validation fails.
    assert not target.exists()


def test_meta_sources_injected_when_model_omits_them(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    """If the LLM forgets ``meta.sources``, the drafter populates it."""
    no_meta = _VALID_DRAFT.replace("meta:\n  sources: [README.md, pyproject.toml]\n", "")
    client = _make_fake_client(no_meta)
    target = tmp_path / "system" / "projects" / "alpha" / "AGENT.md"

    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="proposed",
        client=client,
    )

    assert result.status == "drafted"
    written = target.read_text(encoding="utf-8")
    # `meta.sources:` appears, populated with the actual input paths used.
    assert "meta:" in written
    assert "sources:" in written
    for path in result.input_files_used:
        # Each input path appears in the frontmatter sources list.
        # (path strings include things like "(walk)", "git:meta", etc., so we
        # check the simple ones we know are present.)
        if path in ("README.md", "CLAUDE.md", "pyproject.toml"):
            assert path in written


# ---------------------------------------------------------------------------
# DraftResult shape
# ---------------------------------------------------------------------------


def test_draft_result_carries_token_usage(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    target = tmp_path / "system" / "projects" / "alpha" / "AGENT.md"
    client = _make_fake_client(_VALID_DRAFT)
    result: DraftResult = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="proposed",
        client=client,
    )
    assert result.tokens_input == 100
    assert result.tokens_output == 200
    assert result.model == "claude-sonnet-4-6"
    assert isinstance(result.input_files_used, list) and result.input_files_used


def test_unknown_policy_returns_failure(
    tmp_path: Path,
    fixture_project: Path,
    schema_file: Path,
) -> None:
    target = tmp_path / "out" / "AGENT.md"
    client = _make_fake_client(_VALID_DRAFT)
    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        overwrite_policy="rewrite",  # type: ignore[arg-type]
        client=client,
    )
    assert result.status == "failed"
    assert result.reason is not None
    assert "overwrite_policy" in result.reason


def test_missing_project_path_returns_failure(
    tmp_path: Path,
    schema_file: Path,
) -> None:
    target = tmp_path / "out" / "AGENT.md"
    client = _make_fake_client(_VALID_DRAFT)
    result = draft_agent_md(
        project_path=tmp_path / "does-not-exist",
        output_path=target,
        client=client,
    )
    assert result.status == "failed"
    assert result.reason is not None
    assert "not a directory" in result.reason


# ---------------------------------------------------------------------------
# Smoke: the user-message builder doesn't blow up on weird content
# ---------------------------------------------------------------------------


def test_call_builds_user_message_without_raising(
    fixture_project: Path,
    schema_file: Path,
) -> None:
    """Ensure the LLM-call wrapper feeds the mock client and returns text."""

    def _fake_create(**kwargs: Any) -> _FakeResponse:
        # The system prompt should arrive as a list with cache_control.
        sys_blocks = kwargs["system"]
        assert isinstance(sys_blocks, list)
        assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}
        # The user message should mention each input path.
        user_msg = kwargs["messages"][0]["content"]
        assert "project_path:" in user_msg
        return _FakeResponse(
            content=[_FakeBlock(text=_VALID_DRAFT[len("---\n") :])],
            usage=_FakeUsage(),
        )

    client = MagicMock()
    client.messages.create.side_effect = _fake_create

    target = fixture_project.parent / "AGENT.md"
    result = draft_agent_md(
        project_path=fixture_project,
        output_path=target,
        client=client,
    )
    assert result.status == "drafted"

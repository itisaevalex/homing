"""Tests for the classifier cascade.

We mock the anthropic client. No real network call is made by this test
file — the cascade decisions are what we verify.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cabinet.classifier import (
    LLM_VISION_ESCALATE,
    RULE_ACCEPT,
    RULE_SHORT_CIRCUIT,
    classify_unit,
)
from cabinet.rules.base import UnitContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(
    *,
    path: str = "/test/folder",
    kind: str = "folder",
    extensions: dict[str, int] | None = None,
    file_count: int = 10,
    sample_paths: list[str] | None = None,
    sample_exif: dict | None = None,
    sample_contents: dict | None = None,
    parent_name: str = "",
    extra: dict | None = None,
) -> UnitContext:
    return UnitContext(
        path=Path(path),
        kind=kind,
        extensions=extensions or {},
        file_count=file_count,
        total_size=1024,
        date_range=None,
        sample_paths=[Path(p) for p in (sample_paths or [])],
        sample_contents=sample_contents or {},
        sample_exif=sample_exif or {},
        siblings=[],
        parent_name=parent_name,
        extra=extra or {},
    )


def _tool_use_block(class_id: str, confidence: float, evidence: list[dict] | None = None):
    """A SimpleNamespace shaped like an Anthropic SDK tool_use block."""
    return SimpleNamespace(
        type="tool_use",
        name="submit_classification",
        input={
            "class_id": class_id,
            "confidence": confidence,
            "evidence": evidence or [{"source": "llm", "reason": "test"}],
        },
    )


def _make_response(class_id: str, confidence: float, evidence: list[dict] | None = None):
    return SimpleNamespace(content=[_tool_use_block(class_id, confidence, evidence)])


def _make_client_returning(*responses):
    """Build a mock anthropic client whose .messages.create yields each response in order."""
    client = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


# A high-confidence rule context: screenshots folder. ExtensionRule fires at 0.95.
def screenshot_ctx() -> UnitContext:
    return make_ctx(
        path="/Pictures/Screenshots",
        extensions={".png": 90, ".jpg": 5},
        file_count=95,
        parent_name="Screenshots",
    )


# An ambiguous context — no rule fires, forcing escalation.
def ambiguous_ctx() -> UnitContext:
    return make_ctx(
        path="/Documents/random",
        extensions={".pdf": 2, ".docx": 1, ".xlsx": 1, ".txt": 1},
        file_count=5,
        sample_paths=["/Documents/random/notes.txt"],
        sample_contents={Path("/Documents/random/notes.txt"): b"some unsorted notes"},
        parent_name="random",
    )


# ---------------------------------------------------------------------------
# Cascade short-circuits on high-confidence rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_high_confidence_rule_short_circuits_no_llm_call():
    client = MagicMock()
    verdict = classify_unit(screenshot_ctx(), anthropic_client=client)
    assert verdict.class_id == "screenshot-folder"
    assert verdict.confidence >= RULE_SHORT_CIRCUIT
    assert verdict.rule_name == "by_extension"
    # Critical: no LLM call was made.
    client.messages.create.assert_not_called()


@pytest.mark.unit
def test_high_confidence_rule_short_circuits_without_client():
    """Even without a client, the short-circuit path works."""
    verdict = classify_unit(screenshot_ctx())
    assert verdict.class_id == "screenshot-folder"
    assert verdict.confidence >= RULE_SHORT_CIRCUIT


# ---------------------------------------------------------------------------
# LLM cascade — text first, vision escalation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_text_llm_returns_high_confidence_no_vision_call(tmp_path, monkeypatch):
    """Text LLM gives a confident answer — vision must not be invoked."""
    monkeypatch.setenv("CABINET_TAXONOMY_PATH", str(_taxonomy_fixture(tmp_path)))
    client = _make_client_returning(
        _make_response("ebook", 0.85, [{"source": "filename", "reason": "title looks like an ebook"}]),
    )
    verdict = classify_unit(ambiguous_ctx(), anthropic_client=client)
    assert verdict.class_id == "ebook"
    assert verdict.confidence == 0.85
    # Exactly one call (the text one).
    assert client.messages.create.call_count == 1


@pytest.mark.unit
def test_text_llm_low_confidence_escalates_to_vision(tmp_path, monkeypatch):
    """Text below threshold AND vision allowed AND vision targets present → vision is invoked."""
    monkeypatch.setenv("CABINET_TAXONOMY_PATH", str(_taxonomy_fixture(tmp_path)))

    # Build a context with an image sample so vision has something to send.
    img = tmp_path / "sample.jpg"
    _write_tiny_jpeg(img)

    ctx = make_ctx(
        path=str(tmp_path),
        kind="folder",
        extensions={".jpg": 3, ".pdf": 1},  # not 85% jpg, no rule fires confidently
        file_count=4,
        sample_paths=[str(img)],
        parent_name=tmp_path.name,
    )

    client = _make_client_returning(
        # First call (text): low confidence.
        _make_response("unknown", 0.4),
        # Second call (vision): high confidence.
        _make_response("processed-photo", 0.88, [{"source": str(img), "reason": "looks edited"}]),
    )

    verdict = classify_unit(ctx, anthropic_client=client, allow_vision=True)
    assert verdict.class_id == "processed-photo"
    assert verdict.confidence == 0.88
    assert client.messages.create.call_count == 2


@pytest.mark.unit
def test_vision_skipped_when_allow_vision_false(tmp_path, monkeypatch):
    monkeypatch.setenv("CABINET_TAXONOMY_PATH", str(_taxonomy_fixture(tmp_path)))

    img = tmp_path / "sample.jpg"
    _write_tiny_jpeg(img)

    ctx = make_ctx(
        path=str(tmp_path),
        kind="folder",
        extensions={".jpg": 3, ".pdf": 1},
        file_count=4,
        sample_paths=[str(img)],
        parent_name=tmp_path.name,
    )

    client = _make_client_returning(
        _make_response("unknown", 0.3),
    )
    verdict = classify_unit(ctx, anthropic_client=client, allow_vision=False)
    # We get back the text verdict even though it was low-confidence.
    assert verdict.class_id == "unknown"
    # No vision call.
    assert client.messages.create.call_count == 1


@pytest.mark.unit
def test_vision_skipped_when_no_image_targets(tmp_path, monkeypatch):
    """Even with allow_vision=True, if there are no images/PDFs, no vision call."""
    monkeypatch.setenv("CABINET_TAXONOMY_PATH", str(_taxonomy_fixture(tmp_path)))

    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello")

    ctx = make_ctx(
        path=str(tmp_path),
        kind="folder",
        extensions={".txt": 4},
        file_count=4,
        sample_paths=[str(text_file)],
        parent_name=tmp_path.name,
    )

    client = _make_client_returning(_make_response("unknown", 0.3))
    verdict = classify_unit(ctx, anthropic_client=client, allow_vision=True)
    assert verdict.class_id == "unknown"
    assert client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# Failure: no client and rule cascade insufficient
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clear_error_when_no_client_and_rules_insufficient():
    ctx = ambiguous_ctx()
    with pytest.raises(RuntimeError) as excinfo:
        classify_unit(ctx)  # no client
    msg = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert "anthropic_client" in msg


# ---------------------------------------------------------------------------
# Off-taxonomy LLM output is rejected to "unknown"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_off_taxonomy_llm_class_id_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CABINET_TAXONOMY_PATH", str(_taxonomy_fixture(tmp_path)))
    client = _make_client_returning(
        _make_response("not-a-real-class", 0.99),
    )
    verdict = classify_unit(ambiguous_ctx(), anthropic_client=client)
    assert verdict.class_id == "unknown"
    # Evidence should explain why.
    assert any("invalid-class" in src or "non-taxonomy" in reason for src, reason in verdict.evidence)


# ---------------------------------------------------------------------------
# Threshold sanity checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_threshold_constants_are_ordered():
    assert 0.0 < LLM_VISION_ESCALATE <= RULE_ACCEPT < RULE_SHORT_CIRCUIT <= 1.0


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _taxonomy_fixture(tmp_path: Path) -> Path:
    """Minimal taxonomy YAML so the LLM-path tests don't depend on the real config file."""
    p = tmp_path / "content_classes.yaml"
    p.write_text(
        """
version: 1
classes:
  - id: ebook
    description: An ebook.
    migrate_strategy: archive
    typical_signals: []
    sensitive: false
  - id: processed-photo
    description: An edited photo.
    migrate_strategy: keep
    typical_signals: []
    sensitive: false
  - id: unknown
    description: Could not classify.
    migrate_strategy: review
    typical_signals: []
    sensitive: false
""".strip()
    )
    return p


def _write_tiny_jpeg(path: Path) -> None:
    """Create a tiny but valid JPEG so the vision target detection picks it up.

    The classifier uses Path.suffix to detect candidates and Pillow to render
    the thumbnail. We write a real (1x1 white) JPEG so PIL.Image.open works.
    """
    from PIL import Image

    img = Image.new("RGB", (4, 4), color="white")
    img.save(path, format="JPEG")

"""Tests for reconcile.py — parsing the marked-up triage.md.

Goals:
- Parse a unit section with exactly one checkbox marked.
- Reject 0 or >1 checkboxes (with line numbers in the error).
- Parse archive with destination.
- Parse dedupe groups (one keep, rest dedupe-with-target).
- Round-trip with the renderer for non-degenerate inputs.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from cabinet.reconcile import (
    Decision,
    ReconcileException,
    parse_triage,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "triage.md"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.unit
def test_parse_single_unit_with_keep_marked(tmp_path):
    body = """\
# Cabinet triage manifest

## /a/photos
- Classification: `trip-photos` (confidence 0.92, by by_exif)
- 10 files, 1 KB
- Decision (mark ONE):
  - [x] keep
  - [ ] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert decisions == [Decision(unit_path="/a/photos", action="keep", target=None)]


@pytest.mark.unit
def test_parse_archive_with_destination(tmp_path):
    body = """\
## /a/photos
- Decision (mark ONE):
  - [ ] keep
  - [x] archive → /home/u/cabinet-archive/photos/2019/
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "archive"
    assert d.target == "/home/u/cabinet-archive/photos/2019/"


@pytest.mark.unit
def test_parse_archive_without_destination(tmp_path):
    body = """\
## /a/photos
- Decision (mark ONE):
  - [ ] keep
  - [x] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert decisions[0].action == "archive"
    assert decisions[0].target is None


@pytest.mark.unit
def test_parse_review_later_action(tmp_path):
    body = """\
## /a/x
- Decision:
  - [ ] keep
  - [ ] archive
  - [ ] dedupe
  - [x] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert decisions[0].action == "review-later"


@pytest.mark.unit
def test_parse_trash_action(tmp_path):
    body = """\
## /a/x
- Decision:
  - [ ] keep
  - [ ] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [x] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert decisions[0].action == "trash"


@pytest.mark.unit
def test_reject_section_with_zero_checkboxes(tmp_path):
    body = """\
## /a/photos
- Decision:
  - [ ] keep
  - [ ] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    with pytest.raises(ReconcileException) as exc:
        parse_triage(_write(tmp_path, body))
    msg = str(exc.value)
    assert "no checkbox marked" in msg
    assert "/a/photos" in msg


@pytest.mark.unit
def test_reject_section_with_two_checkboxes(tmp_path):
    body = """\
## /a/photos
- Decision:
  - [x] keep
  - [x] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    with pytest.raises(ReconcileException) as exc:
        parse_triage(_write(tmp_path, body))
    msg = str(exc.value)
    assert "checkboxes marked" in msg


@pytest.mark.unit
def test_parse_sensitive_unit_with_warning_marker(tmp_path):
    body = """\
## ⚠ /docs/passport.pdf
- Decision:
  - [ ] keep
  - [x] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert decisions[0].unit_path == "/docs/passport.pdf"
    assert decisions[0].action == "archive"


@pytest.mark.unit
def test_parse_dedupe_group_one_keep_rest_dedupe(tmp_path):
    body = """\
## DEDUPE GROUP `abc123` — 3 copies

Copies (mark exactly one as keep):
- [x] keep: `/a/copy.pdf`
- [ ] keep: `/b/copy.pdf`
- [ ] keep: `/c/copy.pdf`
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert len(decisions) == 3
    by_path = {d.unit_path: d for d in decisions}
    assert by_path["/a/copy.pdf"].action == "keep"
    assert by_path["/b/copy.pdf"].action == "dedupe"
    assert by_path["/b/copy.pdf"].target == "/a/copy.pdf"
    assert by_path["/c/copy.pdf"].action == "dedupe"
    for d in decisions:
        assert d.dedupe_group == "abc123"


@pytest.mark.unit
def test_reject_dedupe_group_with_no_keep_marked(tmp_path):
    body = """\
## DEDUPE GROUP `abc123` — 2 copies
- [ ] keep: `/a/copy.pdf`
- [ ] keep: `/b/copy.pdf`
"""
    with pytest.raises(ReconcileException) as exc:
        parse_triage(_write(tmp_path, body))
    assert "no copy marked keep" in str(exc.value)


@pytest.mark.unit
def test_reject_dedupe_group_with_two_keeps(tmp_path):
    body = """\
## DEDUPE GROUP `abc123` — 2 copies
- [x] keep: `/a/copy.pdf`
- [x] keep: `/b/copy.pdf`
"""
    with pytest.raises(ReconcileException) as exc:
        parse_triage(_write(tmp_path, body))
    assert "marked keep" in str(exc.value)


@pytest.mark.integration
def test_round_trip_with_renderer(tmp_path):
    """Render → mark a checkbox → parse must recover the marked decision."""
    from cabinet.triage import TriageUnit, render_triage_markdown

    units = [
        TriageUnit(
            unit_id="/x",
            path="/x",
            kind="folder",
            classification="trip-photos",
            confidence=0.9,
            evidence_source="by_exif",
            evidence_notes=("test",),
            file_count=10,
            total_size=1000,
        )
    ]
    md = render_triage_markdown(units, generated_at=dt.datetime(2024, 1, 1))
    # Mark "keep" — find the line and change its `[ ]` → `[x]`.
    marked = md.replace("- [ ] keep", "- [x] keep", 1)
    p = _write(tmp_path, marked)
    decisions = parse_triage(p)
    assert len(decisions) == 1
    assert decisions[0].unit_path == "/x"
    assert decisions[0].action == "keep"


@pytest.mark.unit
def test_multiple_sections_accumulate(tmp_path):
    body = """\
## /a
- Decision:
  - [x] keep
  - [ ] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)

## /b
- Decision:
  - [ ] keep
  - [x] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert {d.unit_path: d.action for d in decisions} == {"/a": "keep", "/b": "archive"}


@pytest.mark.unit
def test_skip_unrecognized_top_level_headers(tmp_path):
    body = """\
## How to use

Some prose, no checkboxes that match an action.

## /a
- Decision:
  - [x] keep
  - [ ] archive
  - [ ] dedupe
  - [ ] review later (skip)
  - [ ] trash (move to ~/cabinet-review-pile/)
"""
    decisions = parse_triage(_write(tmp_path, body))
    assert len(decisions) == 1
    assert decisions[0].unit_path == "/a"

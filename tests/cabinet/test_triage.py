"""Tests for triage.py — markdown rendering of classified units.

Goals:
- Markdown structure is stable + parseable.
- Sensitive units are flagged with ⚠ + warning note.
- Dedupe groups render as a single paired section asking which to keep.
- Sort order: by classification, then by size descending within group.
- Idempotent: same input + fixed timestamp → byte-identical output.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from cabinet.triage import (
    ACTION_ARCHIVE,
    ACTION_KEEP,
    ALL_ACTIONS,
    SENSITIVE_CLASSES,
    TriageUnit,
    render_triage_markdown,
    write_triage_report,
)


def _make_unit(**overrides) -> TriageUnit:
    """Build a TriageUnit with sensible defaults; tests override what they need."""
    base = dict(
        unit_id="/u1",
        path="/u1",
        kind="folder",
        classification="trip-photos",
        confidence=0.92,
        evidence_source="by_exif",
        evidence_notes=("EXIF GPS clustered near 37.97N 23.72E",),
        file_count=42,
        total_size=1024 * 1024,
        date_range=(1565000000.0, 1566000000.0),
        suggested_action=ACTION_KEEP,
    )
    base.update(overrides)
    return TriageUnit(**base)


class _StubWorklist:
    def __init__(self, units):
        self._u = list(units)

    def get_triage_units(self):
        return list(self._u)


@pytest.mark.unit
def test_renders_header_with_total_counts_and_size():
    units = [
        _make_unit(unit_id="/a", path="/a", total_size=10 * 1024 * 1024),
        _make_unit(unit_id="/b", path="/b", total_size=20 * 1024 * 1024),
    ]
    out = render_triage_markdown(units, generated_at=dt.datetime(2024, 1, 1, 12, 0, 0))
    assert "# Cabinet triage manifest" in out
    assert "_Generated: 2024-01-01 12:00:00_" in out
    assert "2 units to review" in out
    # 30 MB total — we render with 1 decimal of precision.
    assert "30.0 MB" in out


@pytest.mark.unit
def test_each_unit_has_section_with_all_action_checkboxes():
    units = [_make_unit(path="/photos/greece")]
    out = render_triage_markdown(units, generated_at=dt.datetime(2024, 1, 1))
    assert "## /photos/greece" in out
    for action in ALL_ACTIONS:
        # Each action appears as an unchecked checkbox.
        assert f"- [ ] {action}" in out


@pytest.mark.unit
def test_sensitive_unit_is_flagged_with_warning():
    sens = _make_unit(
        path="/docs/passport-2019.pdf",
        kind="file",
        classification="scan-of-id",
        file_count=1,
        total_size=120_000,
    )
    out = render_triage_markdown([sens], generated_at=dt.datetime(2024, 1, 1))
    assert "⚠" in out
    # Warning note must show up at least once.
    assert "sensitive" in out.lower()


@pytest.mark.unit
def test_sensitive_classes_set_matches_yaml_intent():
    # Defensive: if the YAML is updated to mark something else sensitive,
    # the renderer must follow. This test pins the contract for v0.1.
    expected_minimum = {"scan-of-id", "tax-document", "contract"}
    assert expected_minimum <= set(SENSITIVE_CLASSES)


@pytest.mark.unit
def test_dedupe_group_renders_as_paired_section_with_keep_lines():
    a = _make_unit(unit_id="/a/copy.pdf", path="/a/copy.pdf", duplicate_group="abc123")
    b = _make_unit(unit_id="/b/copy.pdf", path="/b/copy.pdf", duplicate_group="abc123")
    out = render_triage_markdown([a, b], generated_at=dt.datetime(2024, 1, 1))
    assert "DEDUPE GROUP `abc123`" in out
    assert "- [ ] keep: `/a/copy.pdf`" in out
    assert "- [ ] keep: `/b/copy.pdf`" in out


@pytest.mark.unit
def test_singleton_dedupe_group_promoted_to_per_unit_section():
    only = _make_unit(unit_id="/a/x.pdf", path="/a/x.pdf", duplicate_group="zzz")
    out = render_triage_markdown([only], generated_at=dt.datetime(2024, 1, 1))
    assert "DEDUPE GROUP" not in out
    assert "## /a/x.pdf" in out


@pytest.mark.unit
def test_sort_order_classification_then_size_descending():
    # All same classification: bigger one should come first.
    big = _make_unit(unit_id="/big", path="/big", total_size=10_000_000)
    small = _make_unit(unit_id="/small", path="/small", total_size=1_000)
    out = render_triage_markdown([small, big], generated_at=dt.datetime(2024, 1, 1))
    big_idx = out.index("/big")
    small_idx = out.index("/small")
    assert big_idx < small_idx


@pytest.mark.unit
def test_sort_order_groups_by_classification():
    # Within the rendered output, all "trip-photos" should appear together,
    # all "screenshot" together, etc.
    a = _make_unit(unit_id="/a", path="/a", classification="trip-photos", total_size=100)
    b = _make_unit(unit_id="/b", path="/b", classification="screenshot", total_size=50)
    c = _make_unit(unit_id="/c", path="/c", classification="trip-photos", total_size=200)
    out = render_triage_markdown([a, b, c], generated_at=dt.datetime(2024, 1, 1))
    # /a and /c are trip-photos — they should bracket /b only if /b is between them OR before/after as a group.
    a_idx = out.index("## /a")
    c_idx = out.index("## /c")
    b_idx = out.index("## /b")
    # screenshot group must not be sandwiched between two trip-photos units.
    assert not (a_idx < b_idx < c_idx and a_idx < c_idx)


@pytest.mark.unit
def test_archive_action_renders_with_destination_when_suggested():
    u = _make_unit(
        suggested_action=ACTION_ARCHIVE,
        suggested_archive_dest="/home/user/cabinet-archive/photos/2019-greece/",
    )
    out = render_triage_markdown([u], generated_at=dt.datetime(2024, 1, 1))
    assert "archive → /home/user/cabinet-archive/photos/2019-greece/" in out


@pytest.mark.unit
def test_idempotent_render_for_fixed_timestamp():
    units = [_make_unit(path=f"/u{i}", total_size=i * 1000) for i in range(1, 6)]
    ts = dt.datetime(2024, 1, 1, 12, 0, 0)
    a = render_triage_markdown(units, generated_at=ts)
    b = render_triage_markdown(units, generated_at=ts)
    assert a == b


@pytest.mark.integration
def test_write_triage_report_atomically_creates_file(tmp_path):
    units = [_make_unit(path="/x")]
    wl = _StubWorklist(units)
    out = write_triage_report(wl, tmp_path / "triage.md", generated_at=dt.datetime(2024, 1, 1))
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "## /x" in text
    # No leftover .tmp file.
    assert not (tmp_path / "triage.md.tmp").exists()


@pytest.mark.unit
def test_evidence_notes_render_under_evidence_label():
    u = _make_unit(
        path="/a",
        evidence_notes=("EXIF Camera = Sony A6400", "GPS centroid 37.97N"),
    )
    out = render_triage_markdown([u], generated_at=dt.datetime(2024, 1, 1))
    assert "Evidence:" in out
    assert "EXIF Camera = Sony A6400" in out
    assert "GPS centroid" in out

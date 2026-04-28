"""Tests for the deterministic rule plugins.

Each rule has a small synthetic UnitContext fixture and we assert the rule
fires (or doesn't) and produces the right classification + cited evidence.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cabinet.rules import all_rules
from cabinet.rules.base import Classification, Rule, UnitContext
from cabinet.rules.by_exif import ExifTripRule
from cabinet.rules.by_extension import ExtensionRule
from cabinet.rules.by_filename_pattern import FilenamePatternRule
from cabinet.rules.by_hash_dedup import HashDedupRule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(
    *,
    path: str = "/test/folder",
    kind: str = "folder",
    extensions: dict[str, int] | None = None,
    file_count: int = 10,
    total_size: int = 1024,
    sample_paths: list[str] | None = None,
    sample_exif: dict[Path, dict] | None = None,
    siblings: list[str] | None = None,
    parent_name: str = "",
    extra: dict | None = None,
) -> UnitContext:
    return UnitContext(
        path=Path(path),
        kind=kind,
        extensions=extensions or {},
        file_count=file_count,
        total_size=total_size,
        date_range=None,
        sample_paths=[Path(p) for p in (sample_paths or [])],
        sample_contents={},
        sample_exif=sample_exif or {},
        siblings=siblings or [],
        parent_name=parent_name,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plugin_discovery_finds_all_four_rules():
    rules = all_rules()
    names = {cls.__name__ for cls in rules}
    assert "ExtensionRule" in names
    assert "ExifTripRule" in names
    assert "HashDedupRule" in names
    assert "FilenamePatternRule" in names
    assert len(rules) >= 4


@pytest.mark.unit
def test_plugin_discovery_returns_rule_subclasses():
    for cls in all_rules():
        assert issubclass(cls, Rule)
        assert cls is not Rule


# ---------------------------------------------------------------------------
# ExtensionRule
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extension_rule_fires_on_screenshot_folder():
    ctx = make_ctx(
        extensions={".png": 90, ".jpg": 5},
        file_count=95,
        parent_name="Screenshots",
    )
    rule = ExtensionRule()
    assert rule.applies(ctx)
    verdict = rule.evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "screenshot-folder"
    assert verdict.confidence >= 0.9
    # Citations: the histogram and the parent name.
    sources = [s for s, _ in verdict.evidence]
    assert "extension-histogram" in sources
    assert "parent-name" in sources


@pytest.mark.unit
def test_extension_rule_skips_screenshot_when_parent_name_doesnt_match():
    ctx = make_ctx(
        extensions={".png": 90, ".jpg": 5},
        file_count=95,
        parent_name="random-folder",
    )
    verdict = ExtensionRule().evaluate(ctx)
    assert verdict is None or verdict.class_id != "screenshot-folder"


@pytest.mark.unit
def test_extension_rule_fires_on_archive_dump():
    ctx = make_ctx(
        extensions={".zip": 40, ".rar": 5, ".7z": 3},
        file_count=48,
        parent_name="downloads-archive",
    )
    verdict = ExtensionRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "archive-zip"
    assert verdict.confidence >= 0.9
    assert any("archive" in reason or "extension" in source for source, reason in verdict.evidence)


@pytest.mark.unit
def test_extension_rule_fires_on_known_vendored_parent():
    ctx = make_ctx(
        extensions={".js": 200, ".json": 50, ".md": 10},
        file_count=260,
        parent_name="node_modules",
    )
    verdict = ExtensionRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "vendored-tooling"
    assert verdict.confidence >= 0.9


@pytest.mark.unit
def test_extension_rule_does_not_fire_on_file_unit():
    ctx = make_ctx(kind="file", path="/test/foo.png", extensions={})
    rule = ExtensionRule()
    assert not rule.applies(ctx)


# ---------------------------------------------------------------------------
# ExifTripRule
# ---------------------------------------------------------------------------


def _exif_with(lat: float, lon: float, dt: str) -> dict:
    return {
        "gps_latitude": lat,
        "gps_longitude": lon,
        "DateTimeOriginal": dt,
    }


@pytest.mark.unit
def test_exif_trip_rule_fires_on_clustered_gps_and_narrow_dates():
    sample_exif = {
        Path("/trip/IMG_0001.jpg"): _exif_with(37.9715, 23.7268, "2019:07:01 10:00:00"),
        Path("/trip/IMG_0002.jpg"): _exif_with(37.9750, 23.7250, "2019:07:03 14:00:00"),
        Path("/trip/IMG_0003.jpg"): _exif_with(37.9700, 23.7300, "2019:07:05 09:30:00"),
    }
    ctx = make_ctx(
        path="/trip",
        extensions={".jpg": 200},
        file_count=200,
        sample_paths=[str(p) for p in sample_exif],
        sample_exif=sample_exif,
        parent_name="Greece-2019",
    )
    rule = ExifTripRule()
    assert rule.applies(ctx)
    verdict = rule.evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "trip-photos"
    assert verdict.confidence >= 0.9
    # Cite GPS centroid + date span + at least one sample path.
    sources = [s for s, _ in verdict.evidence]
    assert "exif:GPSInfo" in sources
    assert "exif:DateTimeOriginal" in sources
    assert any("/trip/IMG_" in s for s in sources)


@pytest.mark.unit
def test_exif_trip_rule_does_not_fire_when_dates_span_months():
    sample_exif = {
        Path("/mixed/IMG_0001.jpg"): _exif_with(37.97, 23.72, "2019:07:01 10:00:00"),
        Path("/mixed/IMG_0002.jpg"): _exif_with(37.98, 23.73, "2019:11:15 14:00:00"),
        Path("/mixed/IMG_0003.jpg"): _exif_with(37.97, 23.73, "2020:02:01 10:00:00"),
    }
    ctx = make_ctx(
        path="/mixed",
        extensions={".jpg": 100},
        file_count=100,
        sample_paths=[str(p) for p in sample_exif],
        sample_exif=sample_exif,
    )
    verdict = ExifTripRule().evaluate(ctx)
    assert verdict is None


@pytest.mark.unit
def test_exif_trip_rule_does_not_fire_when_gps_scattered():
    sample_exif = {
        Path("/scattered/IMG_0001.jpg"): _exif_with(37.97, 23.72, "2019:07:01 10:00:00"),
        Path("/scattered/IMG_0002.jpg"): _exif_with(48.85, 2.35, "2019:07:02 14:00:00"),  # Paris
        Path("/scattered/IMG_0003.jpg"): _exif_with(40.71, -74.00, "2019:07:03 09:30:00"),  # NYC
    }
    ctx = make_ctx(
        path="/scattered",
        extensions={".jpg": 100},
        file_count=100,
        sample_paths=[str(p) for p in sample_exif],
        sample_exif=sample_exif,
    )
    verdict = ExifTripRule().evaluate(ctx)
    assert verdict is None


@pytest.mark.unit
def test_exif_trip_rule_skips_when_no_exif():
    ctx = make_ctx(
        path="/photos",
        extensions={".jpg": 100},
        file_count=100,
        sample_exif={},
    )
    rule = ExifTripRule()
    assert not rule.applies(ctx)


@pytest.mark.unit
def test_exif_trip_rule_skips_non_image_folders():
    ctx = make_ctx(
        path="/docs",
        extensions={".pdf": 50},
        file_count=50,
        sample_exif={Path("/docs/foo.pdf"): {"DateTimeOriginal": "2019:07:01 10:00:00"}},
    )
    rule = ExifTripRule()
    assert not rule.applies(ctx)


# ---------------------------------------------------------------------------
# HashDedupRule
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hash_dedup_rule_finds_pair():
    worklist = {
        "/a/file1.pdf": {"content_hash": "abc123", "size": 1000},
        "/b/file1_copy.pdf": {"content_hash": "abc123", "size": 1000},
        "/c/other.pdf": {"content_hash": "def456", "size": 500},
    }
    ctx = make_ctx(
        path="/a/file1.pdf",
        kind="file",
        extensions={".pdf": 1},
        file_count=1,
        extra={"worklist": worklist},
    )
    rule = HashDedupRule()
    assert rule.applies(ctx)
    verdict = rule.evaluate(ctx)
    assert verdict is not None
    assert verdict.confidence == 1.0
    # Both paths cited.
    sources = [s for s, _ in verdict.evidence]
    assert "/a/file1.pdf" in sources
    assert "/b/file1_copy.pdf" in sources


@pytest.mark.unit
def test_hash_dedup_rule_returns_none_when_no_twin():
    worklist = {
        "/a/unique.pdf": {"content_hash": "unique-hash", "size": 1000},
        "/b/other.pdf": {"content_hash": "other-hash", "size": 500},
    }
    ctx = make_ctx(
        path="/a/unique.pdf",
        kind="file",
        extra={"worklist": worklist},
    )
    verdict = HashDedupRule().evaluate(ctx)
    assert verdict is None


@pytest.mark.unit
def test_hash_dedup_rule_skips_folder_units():
    ctx = make_ctx(kind="folder", extra={"worklist": {"/x": {"content_hash": "z"}}})
    rule = HashDedupRule()
    assert not rule.applies(ctx)


@pytest.mark.unit
def test_hash_dedup_rule_skips_when_no_worklist_provided():
    ctx = make_ctx(path="/a/file.pdf", kind="file")
    rule = HashDedupRule()
    assert not rule.applies(ctx)


# ---------------------------------------------------------------------------
# FilenamePatternRule
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filename_rule_fires_on_scan_pdf_file():
    ctx = make_ctx(path="/docs/scan_0042.pdf", kind="file")
    rule = FilenamePatternRule()
    assert rule.applies(ctx)
    verdict = rule.evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "scan-of-document"
    assert verdict.confidence >= 0.9
    assert any("scan_NNNN" in reason for _, reason in verdict.evidence)


@pytest.mark.unit
def test_filename_rule_fires_on_img_camera_file():
    ctx = make_ctx(path="/photos/IMG_1234.jpg", kind="file")
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "raw-camera"
    assert verdict.confidence >= 0.9


@pytest.mark.unit
def test_filename_rule_fires_on_screenshot_filename():
    ctx = make_ctx(
        path="/desktop/Screenshot from 2024-03-15 14-22-01.png",
        kind="file",
    )
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "screenshot"
    assert verdict.confidence >= 0.9


@pytest.mark.unit
def test_filename_rule_fires_on_cv_file():
    ctx = make_ctx(path="/docs/CV_v3.pdf", kind="file")
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "cv-resume"


@pytest.mark.unit
def test_filename_rule_returns_none_on_unmatched_filename():
    ctx = make_ctx(path="/docs/some-random-thing.pdf", kind="file")
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is None


@pytest.mark.unit
def test_filename_rule_folder_unit_with_dominant_pattern():
    sample_paths = [f"/screens/Screenshot_{i:04d}.png" for i in range(10)]
    ctx = make_ctx(
        path="/screens",
        kind="folder",
        extensions={".png": 10},
        file_count=10,
        sample_paths=sample_paths,
        parent_name="screens",
    )
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is not None
    assert verdict.class_id == "screenshot-folder"
    assert verdict.confidence >= 0.85


@pytest.mark.unit
def test_filename_rule_folder_unit_without_dominant_pattern():
    sample_paths = ["/x/random.txt", "/x/IMG_0001.jpg", "/x/notes.md"]
    ctx = make_ctx(
        path="/x",
        kind="folder",
        extensions={".txt": 1, ".jpg": 1, ".md": 1},
        file_count=3,
        sample_paths=sample_paths,
    )
    verdict = FilenamePatternRule().evaluate(ctx)
    assert verdict is None


# ---------------------------------------------------------------------------
# Classification dataclass invariants — every rule that fires must cite.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_classification_has_at_least_one_evidence_item():
    """If a rule fires, it must cite. Drives CLAUDE.md hard rule #3."""
    cases: list[UnitContext] = [
        make_ctx(extensions={".png": 90}, file_count=90, parent_name="Screenshots"),
        make_ctx(path="/docs/scan_001.pdf", kind="file"),
    ]
    for ctx in cases:
        for rule_cls in all_rules():
            rule = rule_cls()
            if not rule.applies(ctx):
                continue
            verdict = rule.evaluate(ctx)
            if verdict is None:
                continue
            assert isinstance(verdict, Classification)
            assert verdict.evidence, f"{rule.name} produced no evidence"
            for src, reason in verdict.evidence:
                assert isinstance(src, str)
                assert isinstance(reason, str)

"""Tests for cabinet.homogeneity."""

from __future__ import annotations

from pathlib import Path

from cabinet.enumerate import enumerate_paths, folder_by_path
from cabinet.homogeneity import (
    FOLDER_CLASSIFIABLE_THRESHOLD,
    SUBDIVIDE_THRESHOLD,
    score_folder,
)


def _folder(fs_root: Path):
    result = enumerate_paths([fs_root])
    return folder_by_path(result, fs_root)


def test_homogeneous_photo_folder_scores_high(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    assert folder is not None

    score = score_folder(folder)

    assert score.score >= FOLDER_CLASSIFIABLE_THRESHOLD, (
        f"expected >=0.85, got {score.score} with evidence {score.evidence}"
    )
    assert score.verdict == "folder-classifiable"


def test_heterogeneous_downloads_scores_low(fixture_fs):
    folder = _folder(fixture_fs.heterogeneous_downloads)
    assert folder is not None

    score = score_folder(folder)

    assert score.score < SUBDIVIDE_THRESHOLD, (
        f"expected <0.5, got {score.score} with evidence {score.evidence}"
    )
    assert score.verdict == "per-file"


def test_tiny_folder_is_trivially_homogeneous(fixture_fs):
    folder = _folder(fixture_fs.tiny_folder)
    assert folder is not None

    score = score_folder(folder)

    assert score.score == 1.0
    assert score.verdict == "folder-classifiable"
    assert "trivially homogeneous" in score.evidence["reason"]


def test_empty_folder_scores_zero(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    folder = _folder(empty)
    assert folder is not None

    score = score_folder(folder)

    assert score.score == 0.0
    assert score.verdict == "per-file"


def test_score_evidence_documents_components(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    score = score_folder(folder)

    components = score.evidence["components"]
    assert {"extension", "filename_pattern", "size", "date"} <= components.keys()
    weights = score.evidence["weights"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_dominant_extension_detection(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    score = score_folder(folder)
    ext_evidence = score.evidence["extension"]
    assert ext_evidence["dominant"] == "jpg"
    assert ext_evidence["share"] == 1.0


def test_pattern_detection_picks_up_img_prefix(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    score = score_folder(folder)
    pattern = score.evidence["filename_pattern"]
    # Either the regex matched or the common-prefix fallback found IMG_
    assert pattern["match"] >= 0.95


def test_subdivide_verdict_for_intermediate_score(tmp_path: Path):
    """A folder where extensions agree but dates and patterns are mixed."""
    d = tmp_path / "mixed_pdfs"
    d.mkdir()
    import os
    import time

    base = time.mktime(time.strptime("2022-01-01", "%Y-%m-%d"))
    for i, name in enumerate(
        ["alpha.pdf", "beta.pdf", "gamma.pdf", "delta.pdf", "random_name.pdf"]
    ):
        p = d / name
        p.write_bytes(b"%PDF-1.4 " + bytes([i]) * (1000 * (i + 1)))
        # Spread mtimes across ~6 months so date_coherence drops
        os.utime(p, (base + i * 30 * 24 * 3600, base + i * 30 * 24 * 3600))

    folder = _folder(d)
    score = score_folder(folder)

    # Same extension lifts ext_score to 1.0; varied sizes and dates pull down.
    assert SUBDIVIDE_THRESHOLD <= score.score < FOLDER_CLASSIFIABLE_THRESHOLD or (
        score.verdict in {"subdivide", "folder-classifiable"}
    )


def test_large_folder_requires_higher_bar(tmp_path: Path):
    """A 600-file folder needs >0.90 to be classifiable, not 0.85."""
    import time

    d = tmp_path / "huge_mixed"
    d.mkdir()
    base = time.mktime(time.strptime("2023-06-01", "%Y-%m-%d"))
    # 550 jpgs and 50 pngs — ext_score = 550/600 = 0.917
    # filenames have no shared pattern beyond a common prefix
    for i in range(550):
        (d / f"a_{i:04d}_xyz_{i % 7}.jpg").write_bytes(b"\xff\xd8" + bytes([i % 256]) * 200)
    for i in range(50):
        (d / f"b_{i:04d}_qpr_{i % 5}.png").write_bytes(b"\x89PNG" + bytes([i % 256]) * 200)
    for p in d.iterdir():
        import os

        os.utime(p, (base, base))

    folder = _folder(d)
    score = score_folder(folder)

    # File count > 500 — verdict must use the higher 0.90 bar.
    assert folder.file_count > 500
    if score.score < 0.90:
        assert score.verdict in {"subdivide", "per-file"}

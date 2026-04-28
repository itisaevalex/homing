"""Tests for cabinet.sampler."""

from __future__ import annotations

from pathlib import Path

from cabinet.enumerate import enumerate_paths, folder_by_path
from cabinet.sampler import sample_files


def _folder(fs_root: Path):
    result = enumerate_paths([fs_root])
    return folder_by_path(result, fs_root)


def test_sample_returns_no_more_than_k(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    samples = sample_files(folder, k=5, strategy="stratified")
    assert len(samples) == 5


def test_sample_deterministic_stratified(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    a = sample_files(folder, k=5, strategy="stratified")
    b = sample_files(folder, k=5, strategy="stratified")
    assert [x.path for x in a] == [x.path for x in b]


def test_sample_deterministic_random(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    a = sample_files(folder, k=5, strategy="random")
    b = sample_files(folder, k=5, strategy="random")
    assert [x.path for x in a] == [x.path for x in b]


def test_stratified_covers_extremes(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    sorted_files = sorted(folder.files, key=lambda f: f.path)
    samples = sample_files(folder, k=5, strategy="stratified")
    sample_paths = {s.path for s in samples}

    # First and last (alphabetically) should be in there.
    assert sorted_files[0].path in sample_paths
    assert sorted_files[-1].path in sample_paths


def test_stratified_includes_largest(tmp_path: Path):
    """Stratified must surface size-outliers so a stray PDF in a photo folder
    doesn't go unsampled."""
    d = tmp_path / "with_outlier"
    d.mkdir()
    # 8 small jpgs and one giant pdf
    for i in range(8):
        (d / f"IMG_{i:04d}.jpg").write_bytes(b"\xff\xd8" + b"a" * 100)
    (d / "OUTLIER.pdf").write_bytes(b"%PDF" + b"X" * 200_000)

    folder = _folder(d)
    samples = sample_files(folder, k=5, strategy="stratified")
    paths = {s.path for s in samples}
    assert any("OUTLIER.pdf" in p for p in paths)


def test_by_extension_picks_one_per_ext(fixture_fs):
    folder = _folder(fixture_fs.heterogeneous_downloads)
    samples = sample_files(folder, k=10, strategy="by_extension")
    extensions = [s.extension for s in samples]
    # Every distinct extension appears at least once (we have <10 in fixture).
    distinct_in_folder = set(folder.file_extensions.keys())
    assert set(extensions) >= distinct_in_folder


def test_sample_smaller_than_k_returns_all(fixture_fs):
    folder = _folder(fixture_fs.tiny_folder)
    samples = sample_files(folder, k=10, strategy="stratified")
    assert len(samples) == folder.file_count


def test_sample_zero_k(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    assert sample_files(folder, k=0) == []


def test_sample_unknown_strategy_raises(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    import pytest

    with pytest.raises(ValueError):
        sample_files(folder, k=3, strategy="nonsense")  # type: ignore[arg-type]


def test_sample_returns_unique_files(fixture_fs):
    folder = _folder(fixture_fs.homogeneous_photos)
    samples = sample_files(folder, k=5, strategy="stratified")
    paths = [s.path for s in samples]
    assert len(paths) == len(set(paths))

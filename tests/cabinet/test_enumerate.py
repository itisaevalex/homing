"""Tests for cabinet.enumerate."""

from __future__ import annotations

from pathlib import Path

from cabinet.enumerate import (
    DEFAULT_PRUNE_DIRS,
    HASH_SIZE_LIMIT_BYTES,
    enumerate_paths,
    folder_by_path,
)


def test_enumerate_homogeneous_photos(fixture_fs):
    # Arrange
    root = fixture_fs.homogeneous_photos

    # Act
    result = enumerate_paths([root])

    # Assert
    folder = folder_by_path(result, root)
    assert folder is not None
    assert folder.file_count == 50
    assert folder.depth == 0
    assert "jpg" in folder.file_extensions
    assert folder.file_extensions["jpg"] == 50
    # All files are sorted by path
    paths = [f.path for f in folder.files]
    assert paths == sorted(paths)


def test_enumerate_is_idempotent(fixture_fs):
    # Arrange
    root = fixture_fs.homogeneous_photos

    # Act
    a = enumerate_paths([root])
    b = enumerate_paths([root])

    # Assert: byte-identical when serialized
    assert a.to_dict() == b.to_dict()


def test_enumerate_prunes_machinery_dirs(fixture_fs):
    # Arrange
    nested = fixture_fs.nested_root

    # Act
    result = enumerate_paths([nested])

    # Assert: no folder paths inside .git or node_modules survive
    for folder in result.folders:
        assert ".git" not in Path(folder.path).parts
        assert "node_modules" not in Path(folder.path).parts

    # And the prune list is the contract
    assert ".git" in DEFAULT_PRUNE_DIRS
    assert "node_modules" in DEFAULT_PRUNE_DIRS

    # Skipped contains the pruned roots
    skipped_str = "\n".join(result.skipped)
    assert ".git" in skipped_str
    assert "node_modules" in skipped_str


def test_enumerate_respects_max_depth(fixture_fs):
    # Arrange
    nested = fixture_fs.nested_root

    # Act: with max_depth=1, the c/ folder (depth 3) should be pruned
    result = enumerate_paths([nested], max_depth=1)

    # Assert: deepest folder path should not exceed depth 1 from root
    for folder in result.folders:
        rel = Path(folder.path).relative_to(nested) if Path(folder.path) != nested else Path(".")
        depth = 0 if str(rel) == "." else len(rel.parts)
        assert depth <= 1


def test_enumerate_skips_symlinks(tmp_path: Path):
    # Arrange
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi")
    link = src / "link_to_a"
    link.symlink_to(src / "a.txt")

    # Act
    result = enumerate_paths([src])

    # Assert
    folder = folder_by_path(result, src)
    assert folder is not None
    file_names = [Path(f.path).name for f in folder.files]
    assert "a.txt" in file_names
    assert "link_to_a" not in file_names
    assert any("link_to_a" in s for s in result.skipped)


def test_enumerate_hashes_small_files(fixture_fs):
    # Arrange / Act
    result = enumerate_paths([fixture_fs.tiny_folder])

    # Assert: small files get real sha256 hashes (64 hex chars)
    folder = folder_by_path(result, fixture_fs.tiny_folder)
    assert folder is not None
    for f in folder.files:
        assert len(f.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in f.content_hash)


def test_enumerate_size_only_hash_for_large_files(big_file_dir):
    # Arrange / Act
    result = enumerate_paths([big_file_dir])

    # Assert
    folder = folder_by_path(result, big_file_dir)
    assert folder is not None
    huge = next(f for f in folder.files if Path(f.path).name == "huge.bin")
    assert huge.size > HASH_SIZE_LIMIT_BYTES
    assert huge.content_hash.startswith("size-only:")
    # Small file gets a real hash.
    small = next(f for f in folder.files if Path(f.path).name == "small.txt")
    assert len(small.content_hash) == 64


def test_enumerate_handles_missing_path(tmp_path: Path):
    missing = tmp_path / "does_not_exist"
    result = enumerate_paths([missing])
    assert result.folders == ()
    assert any("does_not_exist" in s for s in result.skipped)


def test_enumerate_extensions_lowercase(tmp_path: Path):
    d = tmp_path / "mixed_case"
    d.mkdir()
    (d / "PHOTO.JPG").write_bytes(b"\xff\xd8")
    (d / "doc.PDF").write_bytes(b"%PDF")

    result = enumerate_paths([d])
    folder = folder_by_path(result, d)
    assert folder is not None
    assert set(folder.file_extensions.keys()) == {"jpg", "pdf"}


def test_enumerate_multiple_roots(fixture_fs):
    result = enumerate_paths(
        [fixture_fs.homogeneous_photos, fixture_fs.heterogeneous_downloads]
    )
    paths = {f.path for f in result.folders}
    assert str(fixture_fs.homogeneous_photos) in paths
    assert str(fixture_fs.heterogeneous_downloads) in paths

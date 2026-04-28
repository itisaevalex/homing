"""Shared fixture filesystem helpers for cabinet tests.

These helpers build a synthetic directory tree under ``tmp_path``. We use real
os/pathlib calls rather than mocking the filesystem — cabinet is a tool that
walks real disks, so the tests should walk real disks too.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class FixtureFS:
    """Handle to the synthetic filesystem fixture."""

    root: Path
    homogeneous_photos: Path
    heterogeneous_downloads: Path
    tiny_folder: Path
    nested_root: Path

    def all_roots(self) -> list[Path]:
        return [self.homogeneous_photos, self.heterogeneous_downloads, self.tiny_folder]


def _touch(path: Path, content: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def fixture_fs(tmp_path: Path) -> FixtureFS:
    """Build a deterministic directory tree under tmp_path.

    Layout:
        tmp_path/
            photos_2023/                       homogeneous: 50 IMG_NNNN.jpg, narrow date range
            downloads/                         heterogeneous: pdf/jpg/zip/dmg, wide dates
            tiny/                              2 files
            nested/                            depth fixture (a/b/c/file.txt)
                .git/                          should be pruned by enumerate
                node_modules/                  should be pruned
                a/
                    b/
                        c/
                            note.txt
    """
    base_mtime = time.mktime(time.strptime("2023-08-15", "%Y-%m-%d"))

    # 1) Homogeneous photos: 50 IMG_NNNN.jpg, all close in time, similar size
    photos = tmp_path / "photos_2023"
    photos.mkdir()
    for i in range(50):
        # roughly same size with mild jitter (~3MB ± a few KB)
        body = b"\xff\xd8\xff\xe0" + (b"P" * (3_000_000 + (i * 137) % 4096))
        _touch(photos / f"IMG_{i:04d}.jpg", body, base_mtime + i * 60)

    # 2) Heterogeneous downloads: mixed extensions, wide date range, varied sizes
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    mixed = [
        ("invoice_2022.pdf", b"%PDF-1.4 invoice", base_mtime - 365 * 24 * 3600),
        ("photo_a.jpg", b"\xff\xd8\xff\xe0" + b"X" * 4096, base_mtime),
        ("installer.dmg", b"DMG\x00" + b"Y" * 50_000, base_mtime + 30 * 24 * 3600),
        ("backup.zip", b"PK\x03\x04" + b"Z" * 10_000, base_mtime + 200 * 24 * 3600),
        ("notes.txt", b"hello world", base_mtime - 120 * 24 * 3600),
        ("resume_v3.pdf", b"%PDF-1.4 resume", base_mtime + 60 * 24 * 3600),
        ("screenshot.png", b"\x89PNG\r\n" + b"S" * 8192, base_mtime + 10 * 24 * 3600),
        ("data.csv", b"a,b,c\n1,2,3\n", base_mtime - 200 * 24 * 3600),
    ]
    for name, body, mtime in mixed:
        _touch(downloads / name, body, mtime)

    # 3) Tiny folder: just 2 files
    tiny = tmp_path / "tiny"
    tiny.mkdir()
    _touch(tiny / "a.md", b"# hello", base_mtime)
    _touch(tiny / "b.md", b"# world", base_mtime + 60)

    # 4) Nested folder + machinery dirs that must be pruned
    nested = tmp_path / "nested"
    nested.mkdir()
    _touch(nested / ".git" / "HEAD", b"ref: refs/heads/main\n", base_mtime)
    _touch(nested / "node_modules" / "pkg" / "index.js", b"module.exports={}", base_mtime)
    _touch(nested / "a" / "b" / "c" / "note.txt", b"deep file", base_mtime)

    return FixtureFS(
        root=tmp_path,
        homogeneous_photos=photos,
        heterogeneous_downloads=downloads,
        tiny_folder=tiny,
        nested_root=nested,
    )


@pytest.fixture
def big_file_dir(tmp_path: Path) -> Path:
    """A folder with one normal file and one >50MB file (sparse)."""
    base_mtime = time.mktime(time.strptime("2024-01-01", "%Y-%m-%d"))
    d = tmp_path / "bigs"
    d.mkdir()
    _touch(d / "small.txt", b"small body", base_mtime)
    big = d / "huge.bin"
    # Create a 60MB sparse file so we don't pay the disk cost.
    big.parent.mkdir(parents=True, exist_ok=True)
    with big.open("wb") as fh:
        fh.seek(60 * 1024 * 1024 - 1)
        fh.write(b"\x00")
    os.utime(big, (base_mtime, base_mtime))
    return d

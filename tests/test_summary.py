"""Tests for the deterministic summary module (Phase B).

The fixture filesystem mimics a small but realistic ``$HOME`` shape: a
toolchain dir, a cache dir, a config dir, a personal-data dir, and a project
tree containing one git repo. Tests use real filesystem calls — we only mock
``git`` indirectly by running it against a real (tiny) git init.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from homing import summary as summary_module


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _build_home(root: Path) -> Path:
    """Build a fake $HOME tree under ``root`` and return the home path."""
    home = root / "home"
    home.mkdir()

    # toolchain
    _write(home / "anaconda3" / "lib" / "libfoo.so", b"a" * 1024)
    # cache
    _write(home / ".cache" / "junk.bin", b"b" * 2048)
    # config
    _write(home / ".config" / "app" / "settings.toml", b"c" * 16)
    # personal data
    _write(home / "Documents" / "notes.md", b"d" * 64)
    # project tree with a git repo
    proj = home / "Projects" / "demo"
    proj.mkdir(parents=True)
    _write(proj / "README.md", b"# demo\n")
    _git_init(proj)

    # something unmatched -> "other"
    _write(home / "weirdthing" / "blob.dat", b"z" * 8)

    return home


def _git_init(path: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_summary_produces_overview_md(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    system_dir = tmp_path / "system"

    out_path = summary_module.run(home=home, system_dir=system_dir)

    assert out_path == system_dir / "overview.md"
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")
    # Required section headers.
    for header in (
        "# homing overview",
        "## Footprint",
        "## Size by category",
        "## Git repos",
        "## Oldest active repo",
        "## Top biggest never-touched dirs",
        "## Top surprises",
        "## Bali risk",
    ):
        assert header in content, f"missing section: {header}"
    # ASCII only.
    assert content == content.encode("ascii", errors="replace").decode("ascii"), (
        "overview.md must be ASCII-only"
    )
    # Under 200 lines.
    assert content.count("\n") <= 200, "overview.md exceeded 200 lines"


def test_summary_is_idempotent_modulo_timestamp(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    system_dir = tmp_path / "system"

    out1 = summary_module.run(home=home, system_dir=system_dir).read_text(encoding="utf-8")
    out2 = summary_module.run(home=home, system_dir=system_dir).read_text(encoding="utf-8")

    ts_re = re.compile(r"^- Generated: .+$", flags=re.MULTILINE)
    assert ts_re.sub("- Generated: <ts>", out1) == ts_re.sub("- Generated: <ts>", out2)


def test_summary_categorizes_top_level_dirs(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    system_dir = tmp_path / "system"

    out_path = summary_module.run(home=home, system_dir=system_dir)
    content = out_path.read_text(encoding="utf-8")

    # The category table block lives between the two known anchors.
    cat_block = content.split("## Size by category", 1)[1].split("###", 1)[0]
    # Top-10 by-size table lives in the "### Top 10 dirs by size" section.
    biggest_block = content.split("### Top 10 dirs by size", 1)[1]

    # Categories of our fixture dirs should appear in the by-size table.
    assert re.search(r"\| anaconda3 \| toolchain \|", biggest_block)
    assert re.search(r"\| \.cache \| caches \|", biggest_block)
    assert re.search(r"\| \.config \| config \|", biggest_block)
    assert re.search(r"\| Documents \| personal-data \|", biggest_block)
    assert re.search(r"\| Projects \| project-tree \|", biggest_block)
    assert re.search(r"\| weirdthing \| other \|", biggest_block)

    # The category aggregate table should list at least these category names.
    for cat in ("toolchain", "caches", "config", "personal-data", "project-tree", "other"):
        assert cat in cat_block, f"category '{cat}' missing from category block"


def test_summary_reports_clean_bali_risk_for_repo_with_remote_added(tmp_path: Path) -> None:
    """When the only repo has a remote and is clean, Bali risk should report clean."""
    home = tmp_path / "home"
    home.mkdir()
    proj = home / "Projects" / "demo"
    proj.mkdir(parents=True)
    _write(proj / "README.md", b"# demo\n")
    _git_init(proj)
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin", "https://example.invalid/x.git"],
        check=True,
    )

    out = summary_module.run(home=home, system_dir=tmp_path / "system")
    content = out.read_text(encoding="utf-8")
    bali = content.split("## Bali risk", 1)[1]
    assert "Clean" in bali


@pytest.mark.parametrize(
    "name,expected",
    [
        ("anaconda3", "toolchain"),
        (".nvm", "toolchain"),
        (".cache", "caches"),
        (".docker", "caches"),
        (".config", "config"),
        (".claude", "config"),
        ("Documents", "personal-data"),
        ("Projects", "project-tree"),
        ("repos", "project-tree"),
        ("weirdo", "other"),
    ],
)
def test_categorize_known_names(name: str, expected: str) -> None:
    assert summary_module._categorize(name) == expected

"""Tests for ``homing.enumerate``."""

from __future__ import annotations

from pathlib import Path

import pytest

from homing.enumerate import enumerate_home


@pytest.fixture
def fixture_home(tmp_path: Path) -> Path:
    """Build a small fixture filesystem covering every code path.

    Layout::

        tmp_path/
        ├── work/
        │   └── alpha/                  (project: package.json + .git)
        │       ├── package.json
        │       ├── .git/
        │       └── src/
        │           └── index.js
        ├── nested/
        │   └── beta/                   (project: pyproject.toml in subdir of nested)
        │       ├── pyproject.toml
        │       └── beta/__init__.py
        ├── .config/                    (place: category 'config')
        │   └── settings.toml
        ├── Documents/                  (place: category 'personal-data')
        │   └── notes.md
        ├── .local/                     (place: 'config-and-data-mixed' parent)
        │   └── share/
        │       └── ...                 (no known second-level child here)
        └── decoy_modules/
            └── node_modules/           (pruned by name)
                └── package.json        (must NOT register as project)

    The ``decoy_modules/node_modules/`` is the key prune test: it
    contains a project signal yet must not be enumerated, because
    ``node_modules`` is on the prune list.
    """
    # Project alpha: package.json + .git
    alpha = tmp_path / "work" / "alpha"
    (alpha / "src").mkdir(parents=True)
    (alpha / "package.json").write_text('{"name": "alpha"}')
    (alpha / ".git").mkdir()
    (alpha / "src" / "index.js").write_text("console.log('alpha')\n")

    # Project beta nested under tmp_path/nested
    beta = tmp_path / "nested" / "beta"
    (beta / "beta").mkdir(parents=True)
    (beta / "pyproject.toml").write_text('[project]\nname = "beta"\n')
    (beta / "beta" / "__init__.py").write_text("")

    # Place .config
    config_dir = tmp_path / ".config"
    config_dir.mkdir()
    (config_dir / "settings.toml").write_text("k = 1\n")

    # Place Documents
    docs_dir = tmp_path / "Documents"
    docs_dir.mkdir()
    (docs_dir / "notes.md").write_text("# notes\n")

    # Mixed-container parent: .local
    local_dir = tmp_path / ".local"
    (local_dir / "share").mkdir(parents=True)

    # Decoy node_modules (must be pruned; package.json inside should NOT
    # register as project).
    decoy = tmp_path / "decoy_modules" / "node_modules"
    decoy.mkdir(parents=True)
    (decoy / "package.json").write_text("{}")

    return tmp_path


def _config(home: Path) -> dict:
    """Minimal config covering the fixture's structure."""
    return {
        "platform": "test",
        "home_root": str(home),
        "project_hunter": {
            "prune_directories": ["node_modules", ".git"],
            "prune_paths": [],
            "project_signals": [
                ".git",
                "package.json",
                "pyproject.toml",
                "setup.py",
                "requirements.txt",
            ],
            "max_depth": 6,
        },
        "place_classifier": {
            "known_places": {
                ".config": "config",
                "Documents": "personal-data",
                ".local": "config-and-data-mixed",
            },
        },
    }


def test_enumerate_finds_two_projects(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))

    project_paths = sorted(p["path"] for p in result["projects"])
    assert project_paths == [
        str(fixture_home / "nested" / "beta"),
        str(fixture_home / "work" / "alpha"),
    ]


def test_enumerate_node_modules_is_not_a_project(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    paths = {p["path"] for p in result["projects"]}
    decoy = fixture_home / "decoy_modules" / "node_modules"
    assert str(decoy) not in paths


def test_enumerate_records_skipped_node_modules(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    skipped_paths = {s["path"] for s in result["skipped"]}
    assert str(fixture_home / "decoy_modules" / "node_modules") in skipped_paths


def test_enumerate_classifies_places(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    by_path = {p["path"]: p["category"] for p in result["places"]}
    assert by_path[str(fixture_home / ".config")] == "config"
    assert by_path[str(fixture_home / "Documents")] == "personal-data"
    assert by_path[str(fixture_home / ".local")] == "config-and-data-mixed"


def test_alpha_signals_include_git_and_package(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    alpha = next(
        p for p in result["projects"] if p["path"] == str(fixture_home / "work" / "alpha")
    )
    assert ".git" in alpha["signals_found"]
    assert "package.json" in alpha["signals_found"]


def test_idempotent_byte_for_byte_modulo_timestamp(fixture_home: Path) -> None:
    """Two runs against the same tree return identical structure."""
    cfg = _config(fixture_home)
    a = enumerate_home(fixture_home, cfg)
    b = enumerate_home(fixture_home, cfg)
    # Strip timestamp, the only intentionally-non-deterministic field.
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b


def test_lists_are_sorted_by_path(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    project_paths = [p["path"] for p in result["projects"]]
    assert project_paths == sorted(project_paths)
    place_paths = [p["path"] for p in result["places"]]
    assert place_paths == sorted(place_paths)


def test_errors_list_present_and_empty_on_clean_tree(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    assert isinstance(result["errors"], list)
    assert result["errors"] == []


def test_top_level_keys_present(fixture_home: Path) -> None:
    result = enumerate_home(fixture_home, _config(fixture_home))
    assert set(result.keys()) >= {
        "generated_at",
        "platform",
        "projects",
        "places",
        "skipped",
        "errors",
    }
    assert result["platform"] == "test"

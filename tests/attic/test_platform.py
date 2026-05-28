"""Unit tests for attic.platform — defaults + layout."""

from __future__ import annotations

import pytest

from attic import platform as _plat


@pytest.mark.unit
def test_detect_returns_known_os():
    assert _plat.detect() in {"linux", "darwin", "windows"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "platform_str,expected",
    [
        ("linux", "linux"),
        ("darwin", "darwin"),
        ("win32", "windows"),
        ("freebsd13", "linux"),  # unknown → linux fallback
    ],
)
def test_detect_branches(monkeypatch, platform_str, expected):
    monkeypatch.setattr(_plat.sys, "platform", platform_str)
    assert _plat.detect() == expected


@pytest.mark.unit
def test_default_system_dir_ends_with_attic():
    assert _plat.default_system_dir().name == "attic"


@pytest.mark.unit
def test_layout_paths_are_under_system_dir(tmp_path):
    sd = tmp_path / "attic"
    assert _plat.config_path(sd) == sd / "config.toml"
    assert _plat.ledgers_dir(sd) == sd / "ledgers"
    assert _plat.plans_dir(sd) == sd / "plans"
    assert _plat.tombstones_dir(sd) == sd / "tombstones"
    assert _plat.staging_dir(sd) == sd / "staging"


@pytest.mark.unit
def test_ensure_layout_creates_all_dirs(tmp_path):
    sd = tmp_path / "attic"
    _plat.ensure_layout(sd)
    for d in (
        _plat.ledgers_dir(sd),
        _plat.plans_dir(sd),
        _plat.tombstones_dir(sd),
        _plat.staging_dir(sd),
    ):
        assert d.is_dir()
    # Idempotent.
    _plat.ensure_layout(sd)

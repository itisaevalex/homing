"""Unit tests for attic.config — TOML round-trip + key fingerprint."""

from __future__ import annotations

import pytest

from attic.config import (
    AtticConfig,
    ConfigError,
    crypt_key_fingerprint,
    load_config,
    write_config,
)

from .conftest import rekey_crypt_remote, requires_rclone


@pytest.mark.unit
def test_config_roundtrip(tmp_path):
    cfg = AtticConfig(
        remote="attic-crypt:bucket/prefix",
        verify_method="download-roundtrip",
        key_fingerprint="abc123",
    )
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded == cfg


@pytest.mark.unit
def test_config_roundtrip_defaults(tmp_path):
    cfg = AtticConfig(remote="r:b")
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.remote == "r:b"
    assert loaded.verify_method == "download-roundtrip"
    assert loaded.key_fingerprint == ""


@pytest.mark.unit
def test_load_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


@pytest.mark.unit
def test_from_dict_missing_remote_raises():
    with pytest.raises(ConfigError):
        AtticConfig.from_dict({"verify_method": "x"})


@requires_rclone
@pytest.mark.integration
def test_key_fingerprint_changes_when_password_changes(crypt_remote):
    cfg_path = crypt_remote["config_path"]
    before = crypt_key_fingerprint("attic-crypt", rclone_config_path=cfg_path)
    assert before and len(before) == 64

    rekey_crypt_remote(crypt_remote, "a-completely-different-passphrase-xyz")
    after = crypt_key_fingerprint("attic-crypt", rclone_config_path=cfg_path)
    assert after != before


@requires_rclone
@pytest.mark.integration
def test_key_fingerprint_unknown_remote_raises(crypt_remote):
    with pytest.raises(ConfigError):
        crypt_key_fingerprint("does-not-exist", rclone_config_path=crypt_remote["config_path"])


@requires_rclone
@pytest.mark.integration
def test_key_fingerprint_non_crypt_remote_raises(crypt_remote):
    # atticlocal is a plain `local` remote, not crypt.
    with pytest.raises(ConfigError):
        crypt_key_fingerprint("atticlocal", rclone_config_path=crypt_remote["config_path"])

"""Integration tests for attic.transport — needs a live rclone + crypt remote."""

from __future__ import annotations

import pytest

from attic import transport as _transport
from attic.transport import TransportError

from .conftest import requires_rclone

pytestmark = [requires_rclone, pytest.mark.integration]


def _cfg(crypt_remote):
    return crypt_remote["config_path"]


def test_upload_download_file_roundtrip(crypt_remote, tmp_path):
    remote = crypt_remote["remote"]
    src = tmp_path / "f.txt"
    src.write_text("round trip me\n")

    _transport.upload_file(src, remote, "f.txt", rclone_config_path=_cfg(crypt_remote))
    assert _transport.object_exists(remote, "f.txt", rclone_config_path=_cfg(crypt_remote))

    out = tmp_path / "back.txt"
    _transport.download_file(remote, "f.txt", out, rclone_config_path=_cfg(crypt_remote))
    assert out.read_text() == "round trip me\n"


def test_backing_store_is_encrypted(crypt_remote, tmp_path):
    """Plaintext must NOT appear in the backing store (proves crypt is real)."""
    remote = crypt_remote["remote"]
    src = tmp_path / "secret.txt"
    src.write_text("PLAINTEXT-NEEDLE-12345")
    _transport.upload_file(src, remote, "secret.txt", rclone_config_path=_cfg(crypt_remote))
    blobs = b"".join(p.read_bytes() for p in crypt_remote["backing"].rglob("*") if p.is_file())
    assert b"PLAINTEXT-NEEDLE-12345" not in blobs


def test_object_exists_false_for_absent(crypt_remote):
    assert not _transport.object_exists(
        crypt_remote["remote"], "nope.txt", rclone_config_path=_cfg(crypt_remote)
    )


def test_list_objects_empty_for_absent_prefix(crypt_remote):
    # A not-yet-created prefix must list as empty, not raise (refuse-overwrite
    # precheck depends on this).
    assert (
        _transport.list_objects(
            crypt_remote["remote"], "never-made", rclone_config_path=_cfg(crypt_remote)
        )
        == []
    )


def test_upload_dir_and_list_objects_count(crypt_remote, tmp_path):
    remote = crypt_remote["remote"]
    d = tmp_path / "d"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    (d / "sub" / "b.txt").write_text("b")

    count = _transport.upload_dir(
        d, remote, "dprefix", expected_count=2, rclone_config_path=_cfg(crypt_remote)
    )
    assert count == 2
    objects = _transport.list_objects(remote, "dprefix", rclone_config_path=_cfg(crypt_remote))
    assert len(objects) == 2


def test_storage_tier_missing_defaults_standard(crypt_remote, tmp_path):
    remote = crypt_remote["remote"]
    src = tmp_path / "t.txt"
    src.write_text("tier")
    _transport.upload_file(src, remote, "t.txt", rclone_config_path=_cfg(crypt_remote))
    tier = _transport.storage_tier(remote, "t.txt", rclone_config_path=_cfg(crypt_remote))
    assert tier == "STANDARD"
    assert _transport.is_synchronous_tier(tier) is True


def test_is_synchronous_tier_classification():
    assert _transport.is_synchronous_tier("STANDARD")
    assert _transport.is_synchronous_tier("STANDARD_IA")
    assert not _transport.is_synchronous_tier("GLACIER")
    assert not _transport.is_synchronous_tier("DEEP_ARCHIVE")
    assert not _transport.is_synchronous_tier("SOMETHING_UNKNOWN")


def test_nonzero_exit_raises_transport_error(crypt_remote, tmp_path):
    # copyto from a non-existent local source → rclone non-zero exit.
    with pytest.raises(TransportError):
        _transport.upload_file(
            tmp_path / "missing.txt",
            crypt_remote["remote"],
            "x.txt",
            rclone_config_path=_cfg(crypt_remote),
        )


def test_object_stat_absent_raises(crypt_remote):
    with pytest.raises(TransportError):
        _transport.object_stat(
            crypt_remote["remote"], "ghost.txt", rclone_config_path=_cfg(crypt_remote)
        )


def test_rclone_available_and_version():
    assert _transport.rclone_available() is True
    assert _transport.rclone_version().startswith("rclone")

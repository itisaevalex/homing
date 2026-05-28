"""Shared fixtures for attic tests.

The ``crypt_remote`` fixture builds an ISOLATED rclone config under tmp_path: a
``local`` backend plus a ``crypt`` remote wrapping a tmp subdir. Tests inject
the config via the returned ``config_path`` (passed to transport/config as
``rclone_config_path``), so they never touch the user's real rclone config.

The corpus builder includes the round-trip traps: a text file, a binary file, a
file with spaces + non-ASCII name, a 0600-mode file, a SYMLINK, and an EMPTY
DIR — the things object stores and naive transfer drop.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RCLONE = shutil.which("rclone")
requires_rclone = pytest.mark.skipif(RCLONE is None, reason="rclone not on PATH")


def _rclone(config_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["RCLONE_CONFIG"] = str(config_path)
    return subprocess.run(["rclone", *args], capture_output=True, text=True, env=env, check=True)


@pytest.fixture
def crypt_remote(tmp_path: Path) -> dict:
    """An isolated crypt-over-local rclone remote.

    Returns ``{remote, backing, config_path, remote_name, password}``. ``remote``
    is the crypt scheme+base (``attic-crypt:vault``); ``backing`` is the dir that
    holds only encrypted blobs.
    """
    if RCLONE is None:
        pytest.skip("rclone not on PATH")
    config_path = tmp_path / "rclone.conf"
    config_path.touch()
    backing = tmp_path / "backing"
    backing.mkdir()

    password = "attic-test-passphrase-1234567890"
    obscured = _rclone(config_path, "obscure", password).stdout.strip()

    _rclone(config_path, "config", "create", "atticlocal", "local")
    # Use key=value form so an obscured password that begins with "-" isn't
    # parsed by rclone as a flag.
    _rclone(
        config_path,
        "config",
        "create",
        "attic-crypt",
        "crypt",
        f"remote=atticlocal:{backing}",
        f"password={obscured}",
    )
    return {
        "remote": "attic-crypt:vault",
        "remote_name": "attic-crypt",
        "backing": backing,
        "config_path": config_path,
        "password": password,
    }


def rekey_crypt_remote(crypt: dict, new_password: str) -> None:
    """Re-key the crypt remote in place (used to test fingerprint refusal)."""
    obscured = _rclone(crypt["config_path"], "obscure", new_password).stdout.strip()
    _rclone(
        crypt["config_path"],
        "config",
        "update",
        "attic-crypt",
        f"password={obscured}",
    )


def build_corpus(root: Path) -> dict[str, Path]:
    """Create a diverse corpus under ``root``; return a name → path map.

    Includes the round-trip traps: empty dir, symlink, non-ASCII + spaces in
    names, a 0600-mode file, text + binary.
    """
    root.mkdir(parents=True, exist_ok=True)

    text = root / "notes.txt"
    text.write_text("hello attic\nsecond line\n")

    binary = root / "blob.bin"
    binary.write_bytes(bytes((i * 13) % 256 for i in range(8192)))

    weird = root / "Mémo café 2019.txt"
    weird.write_text("naïve café — déjà vu\n")
    weird.chmod(0o644)

    secret = root / "secret.key"
    secret.write_bytes(b"SECRET-KEY-MATERIAL")
    secret.chmod(0o600)

    # A subdir with files, an empty dir, and a symlink — the dir round-trip unit.
    tree = root / "tree"
    (tree / "inner").mkdir(parents=True)
    (tree / "inner" / "a.txt").write_text("inner a\n")
    (tree / "inner" / "b.bin").write_bytes(b"\x00\x01\x02\x03inner-b")
    (tree / "emptydir").mkdir()
    os.symlink("inner/a.txt", tree / "link_to_a")

    return {
        "text": text,
        "binary": binary,
        "weird": weird,
        "secret": secret,
        "tree": tree,
    }

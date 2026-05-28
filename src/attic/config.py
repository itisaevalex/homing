"""attic config — load/write ``config.toml`` + crypt key fingerprint.

The config records which remote evictions target, the verification method, and
a ``key_fingerprint`` — a sha256 over the crypt remote's obscured password and
salt as reported by ``rclone config dump``. The fingerprint lets attic detect a
key/config swap: if the loaded crypt config no longer matches the fingerprint
recorded at push time, restore/evict refuse rather than trusting a remote that
may now be unrecoverable.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VERIFY_METHOD: str = "download-roundtrip"


class ConfigError(Exception):
    """Raised when config is missing, malformed, or references unknown remotes."""


@dataclass(frozen=True, slots=True)
class AtticConfig:
    """attic configuration, persisted as ``config.toml``."""

    remote: str
    verify_method: str = DEFAULT_VERIFY_METHOD
    key_fingerprint: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> AtticConfig:
        if "remote" not in d:
            raise ConfigError("config missing required field: remote")
        return AtticConfig(
            remote=str(d["remote"]),
            verify_method=str(d.get("verify_method", DEFAULT_VERIFY_METHOD)),
            key_fingerprint=str(d.get("key_fingerprint", "")),
        )


# ---------------------------------------------------------------------------
# TOML I/O
# ---------------------------------------------------------------------------


def _dump_toml(d: dict) -> str:
    """Serialize a flat ``str -> str`` mapping to TOML.

    Uses ``tomli_w`` when available; otherwise a minimal hand-written writer.
    attic config is intentionally flat (no nested tables), so the manual path
    stays trivially correct.
    """
    try:
        import tomli_w

        return tomli_w.dumps(d)
    except ImportError:  # pragma: no cover - exercised only when tomli_w absent
        lines: list[str] = []
        for key, value in d.items():
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        return "\n".join(lines) + "\n"


def write_config(config: AtticConfig, path: Path) -> Path:
    """Persist config to ``path`` atomically (tmp + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _dump_toml(config.to_dict())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def load_config(path: Path) -> AtticConfig:
    """Reload config from disk. Round-trips with ``write_config``."""
    if not path.exists():
        raise ConfigError(f"no config at {path}. Run `attic init` first.")
    with path.open("rb") as fh:
        payload = tomllib.load(fh)
    return AtticConfig.from_dict(payload)


# ---------------------------------------------------------------------------
# Crypt key fingerprint
# ---------------------------------------------------------------------------


def _rclone_config_dump(rclone_config_path: Path | None = None) -> dict:
    """Run ``rclone config dump`` and parse the JSON.

    Honours an explicit ``rclone_config_path`` via the ``RCLONE_CONFIG`` env so
    tests can inject an isolated config without touching the user's real one.
    """
    env = dict(os.environ)
    if rclone_config_path is not None:
        env["RCLONE_CONFIG"] = str(rclone_config_path)
    try:
        proc = subprocess.run(
            ["rclone", "config", "dump"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ConfigError("rclone not found on PATH") from exc
    if proc.returncode != 0:
        raise ConfigError(f"rclone config dump failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"could not parse rclone config dump: {exc}") from exc


def crypt_key_fingerprint(
    remote_name: str,
    *,
    rclone_config_path: Path | None = None,
) -> str:
    """sha256 over the crypt remote's key material as rclone reports it.

    The fingerprint hashes the obscured ``password`` and (optional) salt
    ``password2`` plus the underlying ``remote`` string, so a re-key, a salt
    change, or pointing the crypt at a different backing store all change it.
    The obscured password is NOT the plaintext key — it is reversible only with
    rclone's built-in obscure key — but it is sufficient as a stable change
    detector, which is all attic needs here.
    """
    dump = _rclone_config_dump(rclone_config_path)
    if remote_name not in dump:
        raise ConfigError(f"remote not found in rclone config: {remote_name}")
    section = dump[remote_name]
    if section.get("type") != "crypt":
        raise ConfigError(f"remote {remote_name} is not a crypt remote")
    material = "\x00".join(
        [
            section.get("password", ""),
            section.get("password2", ""),
            section.get("remote", ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

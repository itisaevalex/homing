"""CLI-level tests via typer's CliRunner — wiring, gates, and refusal paths.

These drive the real typer commands with an isolated --system-dir and an
injected RCLONE_CONFIG (via env) so they never touch the user's real config.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from attic import platform as _plat
from attic.cli import app

from .conftest import build_corpus, requires_rclone

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure-CLI behaviour (no rclone needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "attic" in result.stdout


@pytest.mark.unit
def test_no_args_shows_help():
    result = runner.invoke(app, [])
    # no_args_is_help → prints help (exit code varies across typer versions).
    assert "Evict cold data" in result.stdout


@pytest.mark.unit
def test_push_refuses_without_confirmed(tmp_path):
    sd = tmp_path / "state"
    result = runner.invoke(
        app, ["push", "--plan", str(tmp_path / "p.json"), "--system-dir", str(sd)]
    )
    assert result.exit_code == 2
    assert "Refusing to push" in result.stdout


@pytest.mark.unit
def test_evict_refuses_without_confirmed(tmp_path):
    result = runner.invoke(
        app, ["evict", "--ledger", str(tmp_path / "l.jsonl"), "--system-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "Refusing to evict" in result.stdout


@pytest.mark.unit
def test_restore_refuses_without_confirmed(tmp_path):
    result = runner.invoke(app, ["restore", "/some/unit", "--system-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "Refusing to restore" in result.stdout


@pytest.mark.unit
def test_plan_without_config_errors(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"x" * 100)
    result = runner.invoke(app, ["plan", str(f), "--system-dir", str(tmp_path / "state")])
    assert result.exit_code == 2  # no config + no --remote


@pytest.mark.unit
def test_plan_with_explicit_remote(tmp_path):
    f = tmp_path / "f.txt"
    f.write_bytes(b"x" * 5000)
    sd = tmp_path / "state"
    result = runner.invoke(
        app,
        [
            "plan",
            str(f),
            "--remote",
            "attic-crypt:vault",
            "--min-size",
            "1000",
            "--system-dir",
            str(sd),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Plan written" in result.stdout
    plans = list((sd / "plans").glob("plan-*.json"))
    assert len(plans) == 1


@pytest.mark.unit
def test_status_empty_state(tmp_path):
    sd = tmp_path / "state"
    _plat.ensure_layout(sd)
    result = runner.invoke(app, ["status", "--system-dir", str(sd)])
    assert result.exit_code == 0
    assert "attic units" in result.stdout


@pytest.mark.unit
def test_push_missing_config_exits(tmp_path):
    sd = tmp_path / "state"
    # plan file exists but no config.toml
    plan_path = tmp_path / "p.json"
    plan_path.write_text('{"schema_version":1,"generated_at":"x","remote":"r:b","actions":[]}')
    result = runner.invoke(
        app, ["push", "--plan", str(plan_path), "--confirmed", "--system-dir", str(sd)]
    )
    assert result.exit_code == 2
    assert "no config" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Full CLI flow against a live crypt remote.
# ---------------------------------------------------------------------------


@requires_rclone
@pytest.mark.integration
def test_init_then_full_flow(crypt_remote, tmp_path, monkeypatch):
    """init (with pre-made --remote) → plan → push → evict → restore → status."""
    monkeypatch.setenv("RCLONE_CONFIG", str(crypt_remote["config_path"]))
    sd = tmp_path / "state"

    # init with an already-configured crypt remote.
    r = runner.invoke(
        app,
        ["init", "--remote", crypt_remote["remote"], "--non-interactive", "--system-dir", str(sd)],
    )
    assert r.exit_code == 0, r.stdout
    assert _plat.config_path(sd).exists()

    # init refuses to overwrite an existing config.
    r = runner.invoke(
        app,
        ["init", "--remote", crypt_remote["remote"], "--non-interactive", "--system-dir", str(sd)],
    )
    assert r.exit_code == 2

    # plan (uses the configured remote).
    corpus = build_corpus(tmp_path / "corpus")
    r = runner.invoke(
        app, ["plan", str(corpus["text"]), str(corpus["tree"]), "--system-dir", str(sd)]
    )
    assert r.exit_code == 0, r.stdout
    plan_path = next((sd / "plans").glob("plan-*.json"))

    # push --confirmed.
    r = runner.invoke(
        app, ["push", "--plan", str(plan_path), "--confirmed", "--system-dir", str(sd)]
    )
    assert r.exit_code == 0, r.stdout
    assert "verified" in r.stdout
    ledger = next((sd / "ledgers").glob("push-*.jsonl"))

    # evict --confirmed → locals gone.
    r = runner.invoke(
        app, ["evict", "--ledger", str(ledger), "--confirmed", "--system-dir", str(sd)]
    )
    assert r.exit_code == 0, r.stdout
    assert not corpus["text"].exists()
    assert not corpus["tree"].exists()

    # status reflects evicted units.
    r = runner.invoke(app, ["status", "--system-dir", str(sd)])
    assert r.exit_code == 0

    # restore --confirmed → byte back.
    unit_id = str(corpus["text"].resolve())
    r = runner.invoke(
        app, ["restore", unit_id, "--ledger", str(ledger), "--confirmed", "--system-dir", str(sd)]
    )
    assert r.exit_code == 0, r.stdout
    assert corpus["text"].exists()
    assert corpus["text"].read_text() == "hello attic\nsecond line\n"


@requires_rclone
@pytest.mark.integration
def test_init_generates_password_when_wrapping_backend(crypt_remote, tmp_path, monkeypatch):
    """init with --remote-backend generates a crypt password and warns loudly."""
    monkeypatch.setenv("RCLONE_CONFIG", str(crypt_remote["config_path"]))
    sd = tmp_path / "state"
    # Wrap the existing 'atticlocal' backend in a fresh crypt remote.
    r = runner.invoke(
        app,
        ["init", "--remote-backend", "atticlocal", "--non-interactive", "--system-dir", str(sd)],
    )
    assert r.exit_code == 0, r.stdout
    assert "TOTAL DATA LOSS" in r.stdout
    assert "password:" in r.stdout
    assert _plat.config_path(sd).exists()


@pytest.mark.unit
def test_init_requires_remote_or_backend(tmp_path, monkeypatch):
    # Skip if rclone absent (init checks rclone availability first).
    import shutil

    if shutil.which("rclone") is None:
        pytest.skip("rclone not on PATH")
    sd = tmp_path / "state"
    r = runner.invoke(app, ["init", "--non-interactive", "--system-dir", str(sd)])
    assert r.exit_code == 2
    assert "Provide --remote" in r.stdout

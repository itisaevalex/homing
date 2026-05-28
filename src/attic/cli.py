"""attic CLI — evict cold data to encrypted object storage, recoverably.

The whole point of attic: ``evict`` deletes a local copy ONLY after a REAL
download -> decrypt -> re-hash round trip proves the bytes are independently
recoverable. The operation logic (``do_push``/``do_evict``/``do_restore`` and
the trusted round-trip gate) lives in ``operations.py``; the typer commands
below are thin ``--confirmed``-gated wrappers, matching cabinet's CLI style.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import TextIO

import typer

from . import __version__
from . import config as _config
from . import ledger as _ledger
from . import plan as _plan
from . import platform as _plat
from . import transport as _transport
from .config import AtticConfig, ConfigError
from .ledger import LedgerEntry
from .operations import EvictionRefused, do_evict, do_push, do_restore

app = typer.Typer(
    name="attic",
    help="Evict cold data to encrypted object storage.",
    no_args_is_help=True,
    add_completion=False,
)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"attic {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool
    | None = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """attic — evict cold data to encrypted object storage, recoverably."""
    return None


def _resolve_system_dir(system_dir: Path | None) -> Path:
    return Path(system_dir) if system_dir else _plat.default_system_dir()


@app.command()
def init(
    remote: str = typer.Option(
        None, "--remote", help="Existing crypt remote, e.g. attic-crypt:bucket/prefix."
    ),
    remote_backend: str = typer.Option(
        None,
        "--remote-backend",
        help="Name of an already-configured backend remote to wrap in crypt.",
    ),
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Scriptable mode (tests/CI)."
    ),
) -> None:
    """Scaffold attic config: ensure rclone, wrap a backend in crypt, store fingerprint."""
    sys_dir = _resolve_system_dir(system_dir)
    _plat.ensure_layout(sys_dir)
    cfg_path = _plat.config_path(sys_dir)

    if not _transport.rclone_available():
        typer.echo("rclone not found on PATH. Install rclone first.")
        raise typer.Exit(code=2)

    if cfg_path.exists():
        typer.echo(f"Config already exists at {cfg_path}. Refusing to overwrite.")
        raise typer.Exit(code=2)

    if remote is None and remote_backend is None:
        typer.echo("Provide --remote <name:bucket/prefix> or --remote-backend <name>.")
        raise typer.Exit(code=2)

    crypt_remote_name = "attic-crypt"
    if remote is None:
        # Wrap an existing backend remote in a fresh crypt remote.
        password = secrets.token_urlsafe(32)
        obscured = _obscure(password)
        _rclone_config_create(
            crypt_remote_name,
            "crypt",
            # key=value form so a "-"-leading obscured password isn't read as a flag.
            [f"remote={remote_backend}:attic", f"password={obscured}"],
        )
        remote = f"{crypt_remote_name}:attic"
        typer.echo("=" * 70)
        typer.echo("!! CRYPT PASSWORD GENERATED — LOSING THIS = TOTAL DATA LOSS !!")
        typer.echo(f"   password: {password}")
        typer.echo(f"   config:   {cfg_path}")
        typer.echo("   Store this password in your password manager NOW.")
        typer.echo("=" * 70)
        # TODO(phase2): paste-back re-key challenge to confirm the user saved it.
    else:
        crypt_remote_name = remote.split(":", 1)[0]

    try:
        fingerprint = _config.crypt_key_fingerprint(crypt_remote_name)
    except ConfigError as exc:
        typer.echo(f"Could not fingerprint crypt remote: {exc}")
        raise typer.Exit(code=2) from exc

    config = AtticConfig(remote=remote, key_fingerprint=fingerprint)
    _config.write_config(config, cfg_path)
    typer.echo(f"attic initialized. Remote: {remote}")
    typer.echo(f"Config written: {cfg_path}")


def _obscure(password: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["rclone", "obscure", password], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise typer.Exit(code=2)
    return proc.stdout.strip()


def _rclone_config_create(name: str, kind: str, params: list[str]) -> None:
    import subprocess

    proc = subprocess.run(
        ["rclone", "config", "create", name, kind, *params],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        typer.echo(f"rclone config create failed: {proc.stderr.strip()}")
        raise typer.Exit(code=2)


@app.command(name="plan")
def plan_cmd(
    paths: list[Path] = typer.Argument(..., help="Paths to consider evicting."),
    min_size: int = typer.Option(0, "--min-size", help="Minimum size in bytes."),
    min_age_days: int = typer.Option(0, "--min-age-days", help="Minimum age in days."),
    remote: str = typer.Option(None, "--remote", help="Override the configured remote."),
    out: Path = typer.Option(None, "--out", help="Output plan path."),
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
) -> None:
    """Build an ArchivePlan from PATHS filtered by size + age."""
    sys_dir = _resolve_system_dir(system_dir)
    _plat.ensure_layout(sys_dir)
    use_remote = remote
    if use_remote is None:
        try:
            use_remote = _config.load_config(_plat.config_path(sys_dir)).remote
        except ConfigError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=2) from exc
    built = _plan.build_plan_from_paths(
        paths, remote=use_remote, min_size=min_size, min_age_days=min_age_days
    )
    ts = built.generated_at.replace(":", "").replace("-", "").replace("Z", "")
    out_path = Path(out) if out else _plat.plans_dir(sys_dir) / f"plan-{ts}.json"
    _plan.write_plan(built, out_path)
    typer.echo(_plan.render_plan(built))
    typer.echo(f"Plan written: {out_path}")


@app.command(name="push")
def push_cmd(
    plan: Path = typer.Option(..., "--plan", help="Path to an ArchivePlan JSON."),
    confirmed: bool = typer.Option(False, "--confirmed", help="Required confirmation."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing remote key."),
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
) -> None:
    """Upload + round-trip-verify a plan. NEVER deletes local data."""
    if not confirmed:
        typer.echo("Refusing to push without --confirmed.")
        raise typer.Exit(code=2)
    sys_dir = _resolve_system_dir(system_dir)
    _plat.ensure_layout(sys_dir)
    config = _load_config_or_exit(sys_dir)
    plan_obj = _plan.load_plan(plan)
    ledger_path = _plat.ledgers_dir(sys_dir) / f"push-{int(time.time())}.jsonl"
    results = do_push(plan_obj, config=config, ledger_path=ledger_path, force=force)
    ok = sum(1 for r in results if r.status == "verified")
    bad = sum(1 for r in results if r.status == "failed")
    typer.echo(f"Pushed + verified: {ok}, failed: {bad}. Ledger: {ledger_path}")
    if bad:
        raise typer.Exit(code=1)


@app.command(name="evict")
def evict_cmd(
    ledger: Path = typer.Option(..., "--ledger", help="Push ledger to evict from."),
    confirmed: bool = typer.Option(False, "--confirmed", help="Required confirmation."),
    allow_cold_evict: bool = typer.Option(
        False, "--allow-cold-evict", help="Allow evicting cold-tier (GLACIER) objects."
    ),
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
) -> None:
    """Free local copies of recoverable units (re-verifies before deleting)."""
    if not confirmed:
        typer.echo("Refusing to evict without --confirmed.")
        raise typer.Exit(code=2)
    sys_dir = _resolve_system_dir(system_dir)
    config = _load_config_or_exit(sys_dir)
    lock = _flock(ledger)
    try:
        results = do_evict(
            ledger, config=config, system_dir=sys_dir, allow_cold_evict=allow_cold_evict
        )
    except EvictionRefused as exc:
        typer.echo(f"Evict refused: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        _funlock(lock)
    purged = sum(1 for r in results if r.status == "purged")
    failed = sum(1 for r in results if r.status == "failed")
    typer.echo(f"Evicted (purged): {purged}, failed: {failed}.")
    if failed:
        raise typer.Exit(code=1)


@app.command(name="restore")
def restore_cmd(
    unit_id: str = typer.Argument(None, help="Unit id (canonical source path) to restore."),
    ledger: Path = typer.Option(None, "--ledger", help="Ledger to read the unit from."),
    to: Path = typer.Option(None, "--to", help="Restore destination (default: original path)."),
    confirmed: bool = typer.Option(False, "--confirmed", help="Required confirmation."),
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
) -> None:
    """Download + verify + atomically place a previously evicted unit."""
    if not confirmed:
        typer.echo("Refusing to restore without --confirmed.")
        raise typer.Exit(code=2)
    sys_dir = _resolve_system_dir(system_dir)
    config = _load_config_or_exit(sys_dir)
    entry = _find_unit_entry(sys_dir, ledger, unit_id)
    if entry is None:
        typer.echo("Could not find a ledger entry for that unit.")
        raise typer.Exit(code=2)
    out_ledger = (
        ledger if ledger else _plat.ledgers_dir(sys_dir) / f"restore-{int(time.time())}.jsonl"
    )
    try:
        do_restore(entry, config=config, ledger_path=out_ledger, to=to)
    except EvictionRefused as exc:
        typer.echo(f"Restore refused: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"Restored {entry.unit_id} → {to or entry.source_path}")


@app.command(name="status")
def status_cmd(
    system_dir: Path = typer.Option(None, "--system-dir", help="attic system dir."),
) -> None:
    """Print each unit's latest op/status/tier/recoverable across all ledgers."""
    from rich.console import Console
    from rich.table import Table

    sys_dir = _resolve_system_dir(system_dir)
    ledgers = sorted(_plat.ledgers_dir(sys_dir).glob("*.jsonl"))
    latest: dict[str, LedgerEntry] = {}
    for lp in ledgers:
        for unit_id, entry in _ledger.latest_by_unit(lp).items():
            cur = latest.get(unit_id)
            if cur is None or entry.timestamp >= cur.timestamp:
                latest[unit_id] = entry

    table = Table(title="attic units", show_header=True, header_style="bold")
    for col in ("unit", "op", "status", "tier", "recoverable"):
        table.add_column(col)
    for unit_id in sorted(latest):
        e = latest[unit_id]
        recoverable = "yes" if e.roundtrip_verified else "?"
        table.add_row(unit_id, e.op, e.status, e.remote_storage_tier or "-", recoverable)
    Console().print(table)


# ---------------------------------------------------------------------------
# Small CLI helpers
# ---------------------------------------------------------------------------


def _load_config_or_exit(sys_dir: Path) -> AtticConfig:
    try:
        return _config.load_config(_plat.config_path(sys_dir))
    except ConfigError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=2) from exc


def _find_unit_entry(sys_dir: Path, ledger: Path | None, unit_id: str | None) -> LedgerEntry | None:
    candidates = [ledger] if ledger else sorted(_plat.ledgers_dir(sys_dir).glob("*.jsonl"))
    best: LedgerEntry | None = None
    for lp in candidates:
        if lp is None or not Path(lp).exists():
            continue
        for uid, entry in _ledger.latest_by_unit(Path(lp)).items():
            if unit_id is not None and uid != unit_id:
                continue
            if best is None or entry.timestamp >= best.timestamp:
                best = entry
    return best


def _flock(path: Path) -> TextIO:
    """Acquire an advisory lock on the ledger (best-effort, POSIX flock)."""
    import fcntl

    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        raise EvictionRefused(f"ledger is locked by another attic process: {path}") from exc
    return fh


def _funlock(fh: TextIO) -> None:
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


if __name__ == "__main__":
    app()

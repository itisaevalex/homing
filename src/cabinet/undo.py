"""Phase F — apply with undo ledger.

This module is the **load-bearing** part of cabinet. It carries the
"don't lose anything" guarantee:

- Source paths are sacred. ``apply_plan`` MOVES (not copies-and-leaves);
  cross-FS uses copy-then-verify-then-remove.
- ``dest`` is never overwritten — if it exists, the action aborts cleanly.
- Every action writes a single JSON-Lines ledger entry BEFORE the action and
  is updated with success status AFTER. The ledger is append-only.
- If any action in a plan fails partway, completed actions are reversed in
  reverse order before the exception is raised — no half-applied plans.
- ``undo_ledger`` reads the ledger in reverse and reverses each completed action,
  verifying byte-identical content before declaring success.

This module is the chaos-test target. Treat every change here as load-bearing.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import shutil
import stat as stat_mod
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .planner import Action, ActionPlan, load_plan

_HASH_CHUNK = 1024 * 1024
LEDGER_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApplyAbort(Exception):
    """Raised when apply must stop without making any further changes."""


class UndoFailure(Exception):
    """Raised when undo cannot fully restore — surfaces partial result."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Pre/post-action fingerprint of a file (or sentinel for a directory).

    ``content_hash`` is sha256 hex for files; ``dir-tree:<hash>`` for dirs
    where the hash is over a deterministic path+content listing.
    """

    path: str
    is_dir: bool
    size: int
    mode: int
    mtime: float
    content_hash: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FileFingerprint":
        return FileFingerprint(**d)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One ledger entry — enough to reverse one completed action.

    Append-only: a "begin" entry is written before the action; a "complete"
    entry with the final state is written after. Both share ``action_id``.
    """

    schema_version: int
    action_id: str
    plan_action: dict          # serialized Action
    pre_state: dict            # FileFingerprint dict
    post_state: dict | None    # FileFingerprint dict, set after success
    status: str                # "begin" | "complete" | "reversed" | "failed"
    timestamp: float

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class UndoResult:
    reversed_count: int
    skipped_count: int
    failures: tuple[str, ...]


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _hash_dir_tree(p: Path) -> str:
    """Hash a directory's content deterministically.

    Symlink policy: NEVER follow. A symlink contributes its target string
    (no recursion through it) regardless of whether it points at a file or
    a dir. This makes the hash invariant under same-FS ``os.rename`` AND
    cross-FS ``shutil.copytree(symlinks=True)`` — both preserve symlinks
    as symlinks, neither resolves them.

    Each entry is recorded with an explicit type prefix so a regular file
    that happens to contain a symlink-shaped string never collides with a
    real symlink at the same path. Mode and mtime are excluded — those are
    captured per-entry in ``_fingerprint`` if needed.
    """
    h = hashlib.sha256()
    files: list[tuple[str, str]] = []
    # os.walk(followlinks=False) is the explicit, deterministic, non-following
    # traversal — pathlib.rglob's symlink behavior changed across Python
    # versions and we don't want our hash depending on which interpreter ran.
    for root, dirs, filenames in os.walk(p, followlinks=False):
        root_path = Path(root)
        # Sort so traversal order is deterministic.
        dirs.sort()
        filenames.sort()
        # Pull symlinked dirs out of the walk so we DON'T recurse into them;
        # record them as symlink entries instead.
        kept_dirs: list[str] = []
        for name in dirs:
            entry = root_path / name
            if entry.is_symlink():
                rel = entry.relative_to(p).as_posix()
                target = os.readlink(entry)
                files.append((rel, f"symlink:{target}"))
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in filenames:
            entry = root_path / name
            rel = entry.relative_to(p).as_posix()
            if entry.is_symlink():
                target = os.readlink(entry)
                files.append((rel, f"symlink:{target}"))
            elif entry.is_file():
                files.append((rel, f"file:{_hash_file(entry)}"))
            # Special files (sockets, fifos, devices) don't migrate;
            # silently ignore them.
    files.sort()
    for rel, ch in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(ch.encode("utf-8"))
        h.update(b"\x00")
    return f"dir-tree:{h.hexdigest()}"


def _fingerprint(path: Path) -> FileFingerprint:
    """Snapshot a file or directory's pre/post-action state.

    A path that is itself a symlink is recorded as a symlink (target string),
    not by following it — same policy as ``_hash_dir_tree``.
    """
    st = path.lstat()
    is_link = stat_mod.S_ISLNK(st.st_mode)
    is_dir = (not is_link) and stat_mod.S_ISDIR(st.st_mode)
    if is_link:
        # Don't follow — the symlink itself is the unit being moved.
        ch = f"symlink:{os.readlink(path)}"
        size = st.st_size
    elif is_dir:
        ch = _hash_dir_tree(path)
        size = 0
    else:
        ch = _hash_file(path)
        size = st.st_size
    return FileFingerprint(
        path=str(path),
        is_dir=is_dir,
        size=size,
        mode=stat_mod.S_IMODE(st.st_mode),
        mtime=st.st_mtime,
        content_hash=ch,
    )


# ---------------------------------------------------------------------------
# Move primitives
# ---------------------------------------------------------------------------


def _same_filesystem(a: Path, b: Path) -> bool:
    """Are two paths on the same filesystem?

    We check the deepest existing ancestor of ``b`` since ``b`` itself does
    not yet exist when we ask.
    """
    try:
        a_dev = a.lstat().st_dev
    except OSError:
        return False
    parent = b
    for _ in range(50):
        if parent.exists():
            break
        if parent.parent == parent:
            break
        parent = parent.parent
    try:
        b_dev = parent.lstat().st_dev
    except OSError:
        return False
    return a_dev == b_dev


def _safe_move(source: Path, dest: Path) -> None:
    """Move ``source`` to ``dest`` safely.

    Contract:
      - ``dest`` must NOT exist (caller guarantees; we re-check).
      - Same FS: ``os.link`` + ``os.unlink`` (or rename for dirs/links).
      - Cross FS: copy-tree → verify content_hash → remove source.
      - On any failure, source is left intact.

    TOCTOU note: POSIX ``rename`` silently overwrites an existing destination,
    so the ``dest.exists()`` check has a race window where a concurrent writer
    could create ``dest`` between the check and the rename. For files on the
    same FS we close that window with ``os.link`` (fails atomically with
    ``EEXIST`` if dest already exists). For directories and symlinks ``link``
    isn't applicable — we re-check immediately before rename to narrow the
    window to a few syscalls; full atomicity there would need ``renameat2``
    which Python doesn't expose.
    """
    if os.path.lexists(dest):
        raise ApplyAbort(f"destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if _same_filesystem(source, dest):
        # Same-FS files: hardlink + unlink for atomic no-overwrite.
        if source.is_file() and not source.is_symlink():
            try:
                os.link(source, dest)
            except FileExistsError as exc:
                raise ApplyAbort(
                    f"destination created by concurrent writer: {dest}"
                ) from exc
            os.unlink(source)
            return
        # Dirs and symlinks: re-check then rename. Narrow but non-zero TOCTOU.
        if os.path.lexists(dest):
            raise ApplyAbort(f"destination created concurrently: {dest}")
        os.rename(source, dest)
        return

    # Cross-FS: copy then verify then remove.
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, dest, symlinks=True)
        # Verify by re-hashing the directory tree on each side; source still
        # exists at this point so it must hash to the same value as dest.
        src_hash = _hash_dir_tree(source)
        dst_hash = _hash_dir_tree(dest)
        if src_hash != dst_hash:
            shutil.rmtree(dest, ignore_errors=True)
            raise ApplyAbort(f"cross-FS copy verify failed (dir): {source} → {dest}")
        shutil.rmtree(source)
    else:
        shutil.copy2(source, dest, follow_symlinks=False)
        try:
            src_hash = _hash_file(source)
            dst_hash = _hash_file(dest)
            if src_hash != dst_hash:
                # Roll back the copy.
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise ApplyAbort(f"cross-FS copy verify failed: {source} → {dest}")
        except OSError as exc:
            try:
                dest.unlink()
            except OSError:
                pass
            raise ApplyAbort(f"cross-FS copy verify failed ({exc}): {source} → {dest}") from exc
        # Now remove the source.
        source.unlink()


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def _append_ledger(ledger_path: Path, entry: LedgerEntry) -> None:
    """Append one JSON-Lines entry. Flushes + fsyncs to survive crashes."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = entry.to_json() + "\n"
    # Open in append mode + binary so we can fsync.
    with ledger_path.open("ab") as fh:
        fh.write(line.encode("utf-8"))
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # On some FS / mocks fsync isn't available — that's a property of
            # the platform, not a correctness issue we can fix here.
            pass


def _read_ledger(ledger_path: Path) -> list[LedgerEntry]:
    """Load all ledger entries in append order."""
    if not ledger_path.exists():
        return []
    out: list[LedgerEntry] = []
    with ledger_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(LedgerEntry(**d))
    return out


# ---------------------------------------------------------------------------
# apply_plan
# ---------------------------------------------------------------------------


def apply_plan(
    plan: ActionPlan,
    *,
    ledger_path: Path,
) -> Path:
    """Execute a plan, recording an undo ledger as we go.

    Behavior:
      - For each "move" action in plan order:
          1. Verify source exists.
          2. Verify dest does NOT exist (never overwrite).
          3. Snapshot pre-state (path, mode, mtime, content hash).
          4. Append a "begin" ledger entry.
          5. Execute the move.
          6. Snapshot post-state.
          7. Append a "complete" ledger entry.
      - "skip" actions: write a "complete" ledger entry with no movement so
        the audit trail is dense, but otherwise do nothing.
      - On any failure: reverse all completed moves in reverse order, then
        re-raise the original error.

    Returns the ledger_path on success.
    """
    completed: list[LedgerEntry] = []
    try:
        for action in plan.actions:
            if action.op == "skip":
                pre = _maybe_fingerprint(Path(action.source))
                entry = LedgerEntry(
                    schema_version=LEDGER_SCHEMA_VERSION,
                    action_id=str(uuid.uuid4()),
                    plan_action=action.to_dict(),
                    pre_state=pre.to_dict() if pre else {"path": action.source, "missing": True},
                    post_state=None,
                    status="complete",
                    timestamp=time.time(),
                )
                _append_ledger(ledger_path, entry)
                completed.append(entry)
                continue

            if action.op != "move":
                raise ApplyAbort(f"unsupported op: {action.op}")

            source = Path(action.source)
            assert action.dest is not None, "move action must have a dest"
            dest = Path(action.dest)

            if not source.exists():
                raise ApplyAbort(f"source does not exist: {source}")
            if dest.exists():
                raise ApplyAbort(f"destination already exists (refusing to overwrite): {dest}")

            pre = _fingerprint(source)
            begin = LedgerEntry(
                schema_version=LEDGER_SCHEMA_VERSION,
                action_id=str(uuid.uuid4()),
                plan_action=action.to_dict(),
                pre_state=pre.to_dict(),
                post_state=None,
                status="begin",
                timestamp=time.time(),
            )
            _append_ledger(ledger_path, begin)

            try:
                _safe_move(source, dest)
            except Exception as exc:
                # Mark the begin entry as failed (append-only, so we add a
                # second entry rather than rewriting).
                fail_entry = LedgerEntry(
                    schema_version=LEDGER_SCHEMA_VERSION,
                    action_id=begin.action_id,
                    plan_action=action.to_dict(),
                    pre_state=pre.to_dict(),
                    post_state=None,
                    status="failed",
                    timestamp=time.time(),
                )
                _append_ledger(ledger_path, fail_entry)
                raise ApplyAbort(f"move failed: {exc}") from exc

            post = _fingerprint(dest)
            complete = LedgerEntry(
                schema_version=LEDGER_SCHEMA_VERSION,
                action_id=begin.action_id,
                plan_action=action.to_dict(),
                pre_state=pre.to_dict(),
                post_state=post.to_dict(),
                status="complete",
                timestamp=time.time(),
            )
            _append_ledger(ledger_path, complete)
            completed.append(complete)

    except ApplyAbort:
        # Roll back completed moves in reverse order.
        _emergency_rollback(completed, ledger_path)
        raise
    except Exception as exc:
        _emergency_rollback(completed, ledger_path)
        raise ApplyAbort(f"unexpected failure: {exc}") from exc

    return ledger_path


def _maybe_fingerprint(p: Path) -> FileFingerprint | None:
    """Fingerprint or return None if the path doesn't exist (skip-action source)."""
    try:
        return _fingerprint(p)
    except FileNotFoundError:
        return None


def _emergency_rollback(completed: Sequence[LedgerEntry], ledger_path: Path) -> None:
    """Reverse completed move actions during a mid-plan failure."""
    for entry in reversed(list(completed)):
        if entry.status != "complete":
            continue
        action = entry.plan_action
        if action.get("op") != "move":
            continue
        source = Path(action["source"])
        dest = Path(action["dest"])
        if not dest.exists():
            # Best-effort — log a "rollback-failed" entry but keep going.
            _append_ledger(
                ledger_path,
                LedgerEntry(
                    schema_version=LEDGER_SCHEMA_VERSION,
                    action_id=entry.action_id,
                    plan_action=action,
                    pre_state=entry.pre_state,
                    post_state=entry.post_state,
                    status="failed",
                    timestamp=time.time(),
                ),
            )
            continue
        try:
            _safe_move(dest, source)
            _append_ledger(
                ledger_path,
                LedgerEntry(
                    schema_version=LEDGER_SCHEMA_VERSION,
                    action_id=entry.action_id,
                    plan_action=action,
                    pre_state=entry.pre_state,
                    post_state=None,
                    status="reversed",
                    timestamp=time.time(),
                ),
            )
        except Exception:  # noqa: BLE001
            _append_ledger(
                ledger_path,
                LedgerEntry(
                    schema_version=LEDGER_SCHEMA_VERSION,
                    action_id=entry.action_id,
                    plan_action=action,
                    pre_state=entry.pre_state,
                    post_state=None,
                    status="failed",
                    timestamp=time.time(),
                ),
            )


# ---------------------------------------------------------------------------
# undo_ledger
# ---------------------------------------------------------------------------


def undo_ledger(ledger_path: Path) -> UndoResult:
    """Reverse every "complete" move in the ledger, in reverse order.

    Verifies the dest's content_hash matches the post_state recorded at apply
    time before reversing — refuses to move modified files back blindly.

    Returns an ``UndoResult`` with counts and any failures.
    """
    entries = _read_ledger(ledger_path)
    # Group by action_id; pick the latest entry per id (the live status).
    latest: dict[str, LedgerEntry] = {}
    order: dict[str, int] = {}
    for idx, e in enumerate(entries):
        latest[e.action_id] = e
        order.setdefault(e.action_id, idx)

    # Order ids by *first* appearance (begin/complete order); reverse for undo.
    ids_in_order = sorted(latest.keys(), key=lambda k: order[k])

    reversed_count = 0
    skipped_count = 0
    failures: list[str] = []

    for aid in reversed(ids_in_order):
        entry = latest[aid]
        if entry.status != "complete":
            skipped_count += 1
            continue
        action = entry.plan_action
        if action.get("op") != "move":
            # Skip actions are no-ops in apply, no-ops in undo.
            skipped_count += 1
            continue

        source = Path(action["source"])
        dest = Path(action["dest"])
        post_state = entry.post_state or {}
        expected_hash = post_state.get("content_hash")

        if not dest.exists():
            failures.append(f"undo {aid}: dest missing — {dest}")
            continue
        if source.exists():
            failures.append(f"undo {aid}: source already exists — refusing to overwrite — {source}")
            continue

        # Verify dest hasn't been modified since apply time.
        try:
            current = _fingerprint(dest)
        except OSError as exc:
            failures.append(f"undo {aid}: cannot fingerprint dest ({exc})")
            continue
        if expected_hash and current.content_hash != expected_hash:
            failures.append(
                f"undo {aid}: dest content changed since apply "
                f"(expected {expected_hash}, got {current.content_hash})"
            )
            continue

        try:
            _safe_move(dest, source)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"undo {aid}: reverse move failed ({exc})")
            continue

        # Restore mode + mtime to pre-state.
        pre = entry.pre_state
        try:
            os.chmod(source, pre["mode"])
            os.utime(source, (pre["mtime"], pre["mtime"]))
        except OSError:
            # Non-fatal — file is back; metadata restoration is best-effort.
            pass

        _append_ledger(
            ledger_path,
            LedgerEntry(
                schema_version=LEDGER_SCHEMA_VERSION,
                action_id=aid,
                plan_action=action,
                pre_state=pre,
                post_state=None,
                status="reversed",
                timestamp=time.time(),
            ),
        )
        reversed_count += 1

    return UndoResult(
        reversed_count=reversed_count,
        skipped_count=skipped_count,
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# CLI registration.
# ---------------------------------------------------------------------------


def register_apply_command(app) -> None:  # pragma: no cover - thin wrapper
    """Register the ``apply`` command on a typer app."""
    import typer

    from . import platform as _plat

    @app.command("apply")
    def apply_cmd(
        confirmed: bool = typer.Option(
            False, "--confirmed", help="Required: explicit confirmation that you reviewed the plan."
        ),
        plan: Path = typer.Option(None, "--plan", help="Path to a plan-*.json file."),
        system_dir: Path = typer.Option(
            None, "--system-dir", "--output-dir", help="Cabinet system dir."
        ),
    ) -> None:
        """Apply a plan with full undo logging. Refuses without --confirmed."""
        if not confirmed:
            typer.echo(
                "Refusing to apply without --confirmed. Review the plan first."
            )
            raise typer.Exit(code=2)
        sys_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
        if plan is None:
            # Pick the most recent plan-*.json in the system dir.
            candidates = sorted(sys_dir.glob("plan-*.json"))
            if not candidates:
                typer.echo("No plan-*.json found. Run `cabinet plan` first.")
                raise typer.Exit(code=2)
            plan = candidates[-1]

        plan_obj = load_plan(plan)
        ledger_id = f"undo-{int(time.time())}"
        ledger_path = sys_dir / "undo" / f"{ledger_id}.jsonl"
        try:
            apply_plan(plan_obj, ledger_path=ledger_path)
        except ApplyAbort as exc:
            typer.echo(f"Apply aborted: {exc}")
            raise typer.Exit(code=1)
        typer.echo(f"Applied. Ledger: {ledger_path}")
        typer.echo(f"Undo with: cabinet undo {ledger_id}")


def register_undo_command(app) -> None:  # pragma: no cover - thin wrapper
    """Register the ``undo`` command on a typer app."""
    import typer

    from . import platform as _plat

    @app.command("undo")
    def undo_cmd(
        ledger_id: str = typer.Argument(..., help="Ledger id to reverse (e.g. undo-1700000000)."),
        system_dir: Path = typer.Option(
            None, "--system-dir", "--output-dir", help="Cabinet system dir."
        ),
    ) -> None:
        """Reverse a previously applied plan from its undo ledger."""
        sys_dir = Path(system_dir) if system_dir else _plat.default_system_dir()
        ledger_path = sys_dir / "undo" / f"{ledger_id}.jsonl"
        if not ledger_path.exists():
            typer.echo(f"No ledger at {ledger_path}")
            raise typer.Exit(code=2)
        result = undo_ledger(ledger_path)
        typer.echo(f"Reversed: {result.reversed_count}, skipped: {result.skipped_count}")
        if result.failures:
            typer.echo("Failures:")
            for f in result.failures:
                typer.echo(f"  - {f}")
            raise typer.Exit(code=1)

"""Phase B — deterministic executive summary.

Produces ``overview.md`` from the filesystem alone, no LLM. The output is
sorted everywhere it could otherwise drift, and (modulo the timestamp line)
running this twice on unchanged input must produce byte-identical output.

The summary answers "what is on this machine" in a form a human can read in
five minutes and an agent can ingest quickly.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Constants — categorisation rules
# ---------------------------------------------------------------------------

# Top-level dir name -> category. Compared after lowercasing and stripping
# leading dot. Anything unmatched falls into "other".
_CATEGORY_BY_NAME: Final[dict[str, str]] = {
    # toolchain
    "anaconda3": "toolchain",
    "miniconda3": "toolchain",
    "nvm": "toolchain",
    "pub-cache": "toolchain",
    "gradle": "toolchain",
    "cargo": "toolchain",
    "rustup": "toolchain",
    "pyenv": "toolchain",
    "rbenv": "toolchain",
    "go": "toolchain",
    "sdkman": "toolchain",
    "deno": "toolchain",
    "bun": "toolchain",
    # caches
    "cache": "caches",
    "npm": "caches",
    "docker": "caches",
    "var": "caches",
    "yarn": "caches",
    "pnpm-store": "caches",
    # config
    "config": "config",
    "claude": "config",
    "cursor": "config",
    "mozilla": "config",
    "ssh": "config",
    "gnupg": "config",
    "vscode": "config",
    # personal data
    "documents": "personal-data",
    "pictures": "personal-data",
    "music": "personal-data",
    "videos": "personal-data",
    "desktop": "personal-data",
    "downloads": "personal-data",
    # project tree
    "projects": "project-tree",
    "repos": "project-tree",
    "code": "project-tree",
    "src": "project-tree",
    "workspace": "project-tree",
    "dev": "project-tree",
}

# Names of categories in the fixed render order.
_CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "toolchain",
    "caches",
    "config",
    "personal-data",
    "project-tree",
    "other",
)

# Directory names to prune when searching for git repos. These are never the
# canonical location of a personal project, and recursing into them blows the
# budget on real $HOME trees.
_GIT_PRUNE_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".cache",
        ".npm",
        ".gradle",
        ".docker",
        "anaconda3",
        "miniconda3",
        ".var",
        "node_modules",
        ".pub-cache",
        ".cargo",
        ".rustup",
        ".nvm",
        ".pnpm-store",
        ".yarn",
    }
)

_GIT_SEARCH_MAX_DEPTH: Final[int] = 6
_BIG_DIR_BYTES: Final[int] = 100 * 1024 * 1024  # 100 MB
_HUGE_DIR_BYTES: Final[int] = 5 * 1024 * 1024 * 1024  # 5 GB
_STALE_DAYS: Final[int] = 90
_VERY_OLD_FILE_DAYS: Final[int] = 5 * 365
_NEVER_TOUCHED_LIMIT: Final[int] = 20
_SURPRISES_LIMIT: Final[int] = 10
_PROJECT_TREE_HINTS: Final[tuple[str, ...]] = ("Programming", "Projects", "repos", "Code", "src")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DirSize:
    name: str
    path: Path
    size_bytes: int
    category: str


@dataclass(frozen=True)
class _RepoInfo:
    path: Path
    has_remote: bool
    is_dirty: bool
    has_unpushed: bool
    last_commit_epoch: int | None  # seconds since epoch; None if no commits


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run(home: Path, system_dir: Path, enumeration: dict | None = None) -> Path:
    """Generate ``overview.md`` for ``home`` under ``system_dir``.

    Args:
        home: The directory to summarize (usually ``$HOME``).
        system_dir: The output directory (usually ``~/system``). Created if missing.
        enumeration: Optional pre-computed enumeration payload. Currently unused
            by this module — included to keep the signature stable for callers
            that wire enumerate -> summary in a single run.

    Returns:
        Absolute path to the written ``overview.md``.
    """
    del enumeration  # reserved for future use
    home = home.resolve()
    system_dir = system_dir.resolve()
    system_dir.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    sections.append(_render_header(home))
    top_level = _list_top_level(home)
    sections.append(_render_disk(home, top_level))
    sections.append(_render_categories(top_level))

    repos = _find_git_repos(home)
    sections.append(_render_git(repos))
    sections.append(_render_oldest_active_repo(repos))

    big_dirs = _collect_big_dirs(home, top_level)
    sections.append(_render_never_touched(big_dirs))
    sections.append(_render_surprises(home, big_dirs))
    sections.append(_render_bali_risk(repos))

    body = "\n\n".join(s.rstrip() for s in sections) + "\n"
    out_path = system_dir / "overview.md"
    out_path.write_text(body, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_header(home: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    host = socket.gethostname() or "unknown-host"
    return (
        "# homing overview\n\n"
        f"- Generated: {now}\n"
        f"- Host: {host}\n"
        f"- Source: {home}\n"
    )


def _render_disk(home: Path, top_level: list[_DirSize]) -> str:
    total_home_bytes = sum(d.size_bytes for d in top_level)
    try:
        usage = shutil.disk_usage(home)
        free_str = _human_bytes(usage.free)
        total_str = _human_bytes(usage.total)
    except OSError:
        free_str = "unknown"
        total_str = "unknown"
    return (
        "## Footprint\n\n"
        f"- Home size: {_human_bytes(total_home_bytes)}\n"
        f"- Top-level entries: {len(top_level)}\n"
        f"- Disk free: {free_str} of {total_str}\n"
    )


def _render_categories(top_level: list[_DirSize]) -> str:
    by_cat: dict[str, int] = {c: 0 for c in _CATEGORY_ORDER}
    counts: dict[str, int] = {c: 0 for c in _CATEGORY_ORDER}
    for d in top_level:
        by_cat[d.category] = by_cat.get(d.category, 0) + d.size_bytes
        counts[d.category] = counts.get(d.category, 0) + 1

    ordered = sorted(
        ((cat, by_cat[cat], counts[cat]) for cat in _CATEGORY_ORDER if counts[cat] > 0),
        key=lambda row: (-row[1], row[0]),
    )

    lines = ["## Size by category", "", "| Category | Size | Top-level dirs |", "|---|---:|---:|"]
    for cat, size, count in ordered:
        lines.append(f"| {cat} | {_human_bytes(size)} | {count} |")

    lines.append("")
    lines.append("### Top 10 dirs by size")
    lines.append("")
    lines.append("| Dir | Category | Size |")
    lines.append("|---|---|---:|")
    biggest = sorted(top_level, key=lambda d: (-d.size_bytes, d.name))[:10]
    for d in biggest:
        lines.append(f"| {d.name} | {d.category} | {_human_bytes(d.size_bytes)} |")
    return "\n".join(lines)


def _render_git(repos: list[_RepoInfo]) -> str:
    total = len(repos)
    with_remote = sum(1 for r in repos if r.has_remote)
    local_only = sum(1 for r in repos if not r.has_remote)
    dirty = sum(1 for r in repos if r.is_dirty)
    unpushed = sum(1 for r in repos if r.has_unpushed)
    return (
        "## Git repos\n\n"
        f"- Total repos: {total}\n"
        f"- With remote: {with_remote}\n"
        f"- Local only: {local_only}\n"
        f"- Dirty (uncommitted changes): {dirty}\n"
        f"- With unpushed commits: {unpushed}\n"
    )


def _render_oldest_active_repo(repos: list[_RepoInfo]) -> str:
    cutoff_active = time.time() - (_STALE_DAYS * 86400)
    active = [r for r in repos if r.last_commit_epoch is not None and r.last_commit_epoch >= cutoff_active]
    if not active:
        return "## Oldest active repo\n\nNo active repos detected (no commits in the last 90 days)."
    # "Oldest among active" = active repo whose last commit is the oldest.
    active.sort(key=lambda r: (r.last_commit_epoch or 0, str(r.path)))
    oldest = active[0]
    when = (
        datetime.fromtimestamp(oldest.last_commit_epoch or 0, tz=timezone.utc).strftime("%Y-%m-%d")
        if oldest.last_commit_epoch
        else "unknown"
    )
    return (
        "## Oldest active repo\n\n"
        f"- Path: {oldest.path}\n"
        f"- Last commit: {when}\n"
    )


def _render_never_touched(big_dirs: list[_DirSize]) -> str:
    cutoff_epoch = time.time() - (_STALE_DAYS * 86400)
    stale: list[_DirSize] = []
    for d in big_dirs:
        if not _has_recent_file(d.path, cutoff_epoch):
            stale.append(d)
    stale.sort(key=lambda d: (-d.size_bytes, str(d.path)))
    stale = stale[:_NEVER_TOUCHED_LIMIT]
    if not stale:
        return (
            "## Top biggest never-touched dirs\n\n"
            "None -- every directory >100MB has been touched in the last 90 days."
        )
    lines = [
        "## Top biggest never-touched dirs",
        "",
        f"Directories larger than 100MB with no file modified in the last {_STALE_DAYS} days.",
        "",
        "| Path | Size |",
        "|---|---:|",
    ]
    for d in stale:
        lines.append(f"| {d.path} | {_human_bytes(d.size_bytes)} |")
    return "\n".join(lines)


def _render_surprises(home: Path, big_dirs: list[_DirSize]) -> str:
    # Surprises bucket A: directories >5GB with category == other.
    huge_other = [
        d for d in big_dirs if d.size_bytes >= _HUGE_DIR_BYTES and d.category == "other"
    ]
    huge_other.sort(key=lambda d: (-d.size_bytes, str(d.path)))

    # Surprises bucket B: very old files in active project trees.
    very_old = _find_very_old_files_in_active(home)

    if not huge_other and not very_old:
        return "## Top surprises\n\nNothing surprising -- no >5GB unclassified dirs and no >5y files in active project trees."

    lines = ["## Top surprises", ""]
    if huge_other:
        lines.append("### Large unclassified dirs (>5GB, category=other)")
        lines.append("")
        lines.append("| Path | Size |")
        lines.append("|---|---:|")
        for d in huge_other[:_SURPRISES_LIMIT]:
            lines.append(f"| {d.path} | {_human_bytes(d.size_bytes)} |")
        lines.append("")
    if very_old:
        very_old.sort(key=lambda row: (row[1], str(row[0])))
        lines.append("### Very old files in active project trees (>5y)")
        lines.append("")
        lines.append("| Path | Last modified |")
        lines.append("|---|---|")
        for path, mtime in very_old[:_SURPRISES_LIMIT]:
            stamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"| {path} | {stamp} |")
    return "\n".join(lines).rstrip()


def _render_bali_risk(repos: list[_RepoInfo]) -> str:
    local_only = sorted([r for r in repos if not r.has_remote], key=lambda r: str(r.path))
    dirty = sorted([r for r in repos if r.is_dirty], key=lambda r: str(r.path))
    if not local_only and not dirty:
        return (
            "## Bali risk\n\n"
            "Clean -- every git repo has a remote and a clean working tree. "
            "Nothing would be lost on a sudden migration."
        )
    lines = [
        "## Bali risk",
        "",
        "Repos that would be at risk in a sudden migration: local-only repos have nowhere to push to, "
        "and dirty repos have uncommitted changes.",
        "",
    ]
    if local_only:
        lines.append("### Local-only repos")
        lines.append("")
        for r in local_only:
            lines.append(f"- {r.path}")
        lines.append("")
    if dirty:
        lines.append("### Dirty repos (uncommitted changes)")
        lines.append("")
        for r in dirty:
            lines.append(f"- {r.path}")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _list_top_level(home: Path) -> list[_DirSize]:
    """Return a deterministic, sorted list of top-level directories with sizes."""
    out: list[_DirSize] = []
    try:
        entries = sorted(home.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, PermissionError):
        return out
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
        except OSError:
            continue
        size = _dir_size(entry)
        out.append(_DirSize(entry.name, entry, size, _categorize(entry.name)))
    return out


def _categorize(name: str) -> str:
    key = name.lstrip(".").lower()
    return _CATEGORY_BY_NAME.get(key, "other")


def _dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False, onerror=lambda _e: None):
        # Stable iteration order so total is deterministic even if the
        # walker's behaviour changes around symlinked/sparse files.
        dirs.sort()
        files.sort()
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            # Skip symlinks: their target size is counted at the target's parent.
            if not _is_regular_file_mode(st.st_mode):
                continue
            total += st.st_size
    return total


def _is_regular_file_mode(mode: int) -> bool:
    import stat as _stat

    return _stat.S_ISREG(mode)


def _find_git_repos(home: Path) -> list[_RepoInfo]:
    """Discover git repos under ``home`` up to depth 6, pruning known noise."""
    found: list[Path] = []
    home = home.resolve()
    home_depth = len(home.parts)
    for root, dirs, _files in os.walk(home, followlinks=False, onerror=lambda _e: None):
        depth = len(Path(root).parts) - home_depth
        if depth >= _GIT_SEARCH_MAX_DEPTH:
            dirs[:] = []
            continue
        # Prune noisy / unwanted subtrees in-place.
        dirs[:] = sorted(d for d in dirs if d not in _GIT_PRUNE_NAMES)
        if ".git" in dirs:
            found.append(Path(root))
            # Don't descend further inside this repo when looking for repos.
            dirs[:] = []
    found.sort(key=str)
    return [_inspect_repo(p) for p in found]


def _inspect_repo(repo_path: Path) -> _RepoInfo:
    has_remote = _git_has_remote(repo_path)
    is_dirty = _git_is_dirty(repo_path)
    has_unpushed = _git_has_unpushed(repo_path) if has_remote else False
    last_commit = _git_last_commit_epoch(repo_path)
    return _RepoInfo(
        path=repo_path,
        has_remote=has_remote,
        is_dirty=is_dirty,
        has_unpushed=has_unpushed,
        last_commit_epoch=last_commit,
    )


def _git(repo: Path, *args: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 1, ""


def _git_has_remote(repo: Path) -> bool:
    code, out = _git(repo, "remote")
    return code == 0 and bool(out)


def _git_is_dirty(repo: Path) -> bool:
    code, out = _git(repo, "status", "--porcelain")
    return code == 0 and bool(out)


def _git_has_unpushed(repo: Path) -> bool:
    code, out = _git(repo, "log", "--branches", "--not", "--remotes", "--oneline")
    return code == 0 and bool(out)


def _git_last_commit_epoch(repo: Path) -> int | None:
    code, out = _git(repo, "log", "-1", "--format=%ct")
    if code != 0 or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _collect_big_dirs(home: Path, top_level: list[_DirSize]) -> list[_DirSize]:
    """Walk top-level dirs and return any sub-tree (or top-level itself) >100MB.

    To stay deterministic and bounded, we only descend two levels under each
    top-level entry, which is enough to surface caches, project subtrees, and
    obvious large hoards while keeping traversal cost predictable.
    """
    out: list[_DirSize] = []
    seen: set[str] = set()
    for top in top_level:
        if top.size_bytes >= _BIG_DIR_BYTES:
            out.append(top)
            seen.add(str(top.path))
        # Level 1 children
        for child in _safe_iter_dirs(top.path):
            if str(child) in seen:
                continue
            size = _dir_size(child)
            if size >= _BIG_DIR_BYTES:
                cat = _categorize_for_subpath(home, child, top.category)
                out.append(_DirSize(child.name, child, size, cat))
                seen.add(str(child))
            # Level 2 children
            for grand in _safe_iter_dirs(child):
                if str(grand) in seen:
                    continue
                gsize = _dir_size(grand)
                if gsize >= _BIG_DIR_BYTES:
                    cat = _categorize_for_subpath(home, grand, top.category)
                    out.append(_DirSize(grand.name, grand, gsize, cat))
                    seen.add(str(grand))
    out.sort(key=lambda d: (-d.size_bytes, str(d.path)))
    return out


def _safe_iter_dirs(path: Path) -> Iterable[Path]:
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return []
    out: list[Path] = []
    for e in entries:
        try:
            if e.is_dir() and not e.is_symlink():
                out.append(e)
        except OSError:
            continue
    return out


def _categorize_for_subpath(home: Path, path: Path, parent_category: str) -> str:
    """Categorise a sub-path. Honour parent category for clarity, but bump
    project-tree paths under known project hints so surprises are accurate.
    """
    try:
        rel_parts = path.relative_to(home).parts
    except ValueError:
        rel_parts = path.parts
    for part in rel_parts:
        if any(hint.lower() in part.lower() for hint in _PROJECT_TREE_HINTS):
            return "project-tree"
    return parent_category


def _has_recent_file(path: Path, cutoff_epoch: float) -> bool:
    """Return True if any regular file under ``path`` has mtime >= cutoff."""
    for root, dirs, files in os.walk(path, followlinks=False, onerror=lambda _e: None):
        dirs.sort()
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not _is_regular_file_mode(st.st_mode):
                continue
            if st.st_mtime >= cutoff_epoch:
                return True
    return False


def _find_very_old_files_in_active(home: Path) -> list[tuple[Path, float]]:
    """Find files older than 5 years inside dirs that look like active projects.

    Active = matches a project-tree hint in its path AND has at least one file
    modified in the last 90 days somewhere under it.
    """
    cutoff_old = time.time() - (_VERY_OLD_FILE_DAYS * 86400)
    cutoff_recent = time.time() - (_STALE_DAYS * 86400)

    candidates: list[Path] = []
    home = home.resolve()
    for top in _safe_iter_dirs(home):
        for hint in _PROJECT_TREE_HINTS:
            if hint.lower() in top.name.lower():
                candidates.append(top)
                break
        else:
            # Also descend one level for hints like "Programming Projects".
            for child in _safe_iter_dirs(top):
                if any(h.lower() in child.name.lower() for h in _PROJECT_TREE_HINTS):
                    candidates.append(child)

    findings: list[tuple[Path, float]] = []
    for root_path in candidates:
        if not _has_recent_file(root_path, cutoff_recent):
            continue
        for root, dirs, files in os.walk(root_path, followlinks=False, onerror=lambda _e: None):
            dirs[:] = sorted(d for d in dirs if d not in _GIT_PRUNE_NAMES)
            for f in sorted(files):
                fp = os.path.join(root, f)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if not _is_regular_file_mode(st.st_mode):
                    continue
                if st.st_mtime <= cutoff_old:
                    findings.append((Path(fp), st.st_mtime))
                    if len(findings) >= _SURPRISES_LIMIT * 4:
                        break
            if len(findings) >= _SURPRISES_LIMIT * 4:
                break
        if len(findings) >= _SURPRISES_LIMIT * 4:
            break
    return findings


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _human_bytes(n: int) -> str:
    """Format ``n`` bytes as a stable human string (powers of 1024)."""
    if n < 1024:
        return f"{n} B"
    units = ("KB", "MB", "GB", "TB", "PB")
    value = float(n)
    unit = "B"
    for u in units:
        value /= 1024.0
        unit = u
        if value < 1024.0:
            break
    return f"{value:.1f} {unit}"

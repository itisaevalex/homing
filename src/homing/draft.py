"""Phase E — LLM-driven AGENT.md drafter.

Generates an ``AGENT.md`` (schema v2) for a single project by:

1. Walking the project tree deterministically and collecting a small,
   capped bundle of ground-truth inputs (README, agent docs, manifests,
   recent git history, env-var names, CI workflows, etc.).
2. Sending the bundle plus the schema to Claude with strong anti-hallucination
   instructions ("cite or write `# TODO`").
3. Validating the LLM output (YAML frontmatter parses, required fields
   present, body sections present, no obvious foreign-OS path leaks).
4. Writing the result to disk under the strict no-silent-overwrite policy
   from ``CLAUDE.md``: existing files become ``*.proposed.md`` siblings.

This module never mutates the source project. It writes only to the
``output_path`` (or its ``.proposed.md`` sibling) under ``~/system/``.

The CLI wiring lives in ``homing.draft_cli`` so this module stays a pure
library — see that file for the ``homing draft`` command.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

OverwritePolicy = str  # "proposed" | "skip" | "fail"

VALID_POLICIES: tuple[str, ...] = ("proposed", "skip", "fail")

REQUIRED_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "name",
    "purpose",
    "state",
    "type",
    "stack",
)

STANDARD_BODY_SECTIONS: tuple[str, ...] = (
    "## What this is",
    "## How to run",
    "## Agent instructions",
    "## Recent work",
    "## Known issues",
)

# Files we treat as authoritative agent-facing source documents.
AGENT_DOC_NAMES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "AGENT.md",
    ".cursorrules",
    ".windsurfrules",
    "SPEC.md",
    "ARCHITECTURE.md",
    "INTENT.md",
)

README_NAMES: tuple[str, ...] = ("README.md", "README.rst", "README.txt", "README")

MANIFEST_NAMES: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "pubspec.yaml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "build.gradle",
    "build.gradle.kts",
)

# Defaults — tunable per call.
_DEFAULT_README_LINES = 200
_DEFAULT_AGENT_DOC_LINES = 100
_DEFAULT_MANIFEST_LINES = 80
# Rough characters-per-token heuristic. Conservative; we'd rather under-pack.
_CHARS_PER_TOKEN = 4

# Schema location candidates (first hit wins).
# The first candidate resolves the schema relative to this file so the package
# works from any clone location. The second is the per-machine homing output
# directory (configurable; ~/system is the default).
_SCHEMA_PATH_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).parent.parent.parent / "config" / "schema" / "AGENT.md.template",
    Path.home() / "system" / "SCHEMA.md",
)


@dataclass
class DraftResult:
    """Outcome of one drafting call.

    ``status`` is one of:

    - ``"drafted"`` — file did not exist, written directly.
    - ``"proposed"`` — file existed; wrote to ``*.proposed.md``.
    - ``"skipped"`` — policy=skip and a file existed; nothing written.
    - ``"failed"`` — pre-flight or validation failed; nothing written.
    """

    project_path: Path
    output_path: Path | None
    status: str
    reason: str | None = None
    input_files_used: list[str] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    model: str = ""


# ---------------------------------------------------------------------------
# Pre-processing — deterministic, no LLM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InputFile:
    """One slice of evidence handed to the LLM."""

    label: str  # short, e.g. "README.md" or "git/log"
    path: str  # repo-relative path or pseudo-path like "git:log"
    content: str

    def char_size(self) -> int:
        return len(self.content)


def _safe_read_text(path: Path, max_lines: int) -> str | None:
    """Read up to ``max_lines`` lines from ``path``; tolerate decode errors.

    Returns ``None`` when the file can't be read at all.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = []
            for i, line in enumerate(fh):
                if i >= max_lines:
                    lines.append(f"... [truncated at {max_lines} lines]\n")
                    break
                lines.append(line)
            return "".join(lines)
    except OSError:
        return None


def _run_git(project_path: Path, args: list[str]) -> str | None:
    """Run a git subcommand inside ``project_path``; return stdout or None."""
    if not (project_path / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _list_top_level(project_path: Path, limit: int = 200) -> list[str]:
    """Sorted list of top-level entries with a trailing ``/`` for dirs."""
    try:
        entries = sorted(project_path.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    out: list[str] = []
    for entry in entries:
        try:
            suffix = "/" if entry.is_dir() else ""
        except OSError:
            suffix = ""
        out.append(entry.name + suffix)
        if len(out) >= limit:
            out.append(f"... [truncated at {limit} entries]")
            break
    return out


def _extract_env_var_names(env_example_text: str) -> list[str]:
    """Return only the variable NAMES from a ``.env.example`` blob.

    We never echo values back, even if they look benign — the prompt
    contract is "names only".
    """
    names: list[str] = []
    name_re = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
    for line in env_example_text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        m = name_re.match(line)
        if m:
            names.append(m.group(1))
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _file_count(project_path: Path, cap: int = 5000) -> int:
    """Approximate file count under ``project_path``, capped for sanity."""
    n = 0
    try:
        for _root, _dirs, files in os.walk(project_path):
            n += len(files)
            if n >= cap:
                return cap
    except OSError:
        pass
    return n


def collect_inputs(
    project_path: Path,
    *,
    max_input_files: int = 25,
    max_input_tokens: int = 50_000,
) -> list[_InputFile]:
    """Build the deterministic input bundle.

    Order is stable: top-listing first, then agent docs, then README,
    manifests, env names, CI names, then git context. Within each
    category files are sorted by name. This makes the LLM input
    byte-identical for byte-identical project trees, which keeps the
    drafter testable.

    Total content is capped at ``max_input_files`` items and
    ``max_input_tokens * _CHARS_PER_TOKEN`` characters.
    """
    project_path = project_path.resolve()
    char_budget = max_input_tokens * _CHARS_PER_TOKEN
    bundle: list[_InputFile] = []
    used_chars = 0

    def _add(label: str, path: str, content: str) -> bool:
        nonlocal used_chars
        if len(bundle) >= max_input_files:
            return False
        if used_chars + len(content) > char_budget:
            # Truncate to fit if at all useful.
            remaining = char_budget - used_chars
            if remaining < 200:
                return False
            content = content[:remaining] + "\n... [truncated to fit token budget]\n"
        bundle.append(_InputFile(label=label, path=path, content=content))
        used_chars += len(content)
        return True

    # 1. Top-level listing.
    listing = _list_top_level(project_path)
    if listing:
        _add(
            "top-level listing",
            "(walk)",
            "\n".join(listing) + "\n",
        )

    # 2. Agent-facing docs at the project root (sorted by AGENT_DOC_NAMES order).
    for name in AGENT_DOC_NAMES:
        path = project_path / name
        if not path.is_file():
            continue
        text = _safe_read_text(path, _DEFAULT_AGENT_DOC_LINES)
        if text is None:
            continue
        if not _add(name, name, text):
            break

    # 3. README at the project root.
    for name in README_NAMES:
        path = project_path / name
        if not path.is_file():
            continue
        text = _safe_read_text(path, _DEFAULT_README_LINES)
        if text is None:
            continue
        if not _add(name, name, text):
            break
        break  # one README is enough

    # 4. Manifests / build config (sorted by canonical order).
    for name in MANIFEST_NAMES:
        path = project_path / name
        if not path.is_file():
            continue
        text = _safe_read_text(path, _DEFAULT_MANIFEST_LINES)
        if text is None:
            continue
        if not _add(name, name, text):
            break

    # 5. .env.example — variable NAMES only.
    env_example_path = project_path / ".env.example"
    if env_example_path.is_file():
        raw = _safe_read_text(env_example_path, 500)
        if raw is not None:
            names = _extract_env_var_names(raw)
            if names:
                names_text = "\n".join(names) + "\n"
                _add(".env.example (names only)", ".env.example", names_text)

    # 6. CI workflow file names (names only — no contents).
    workflows_dir = project_path / ".github" / "workflows"
    if workflows_dir.is_dir():
        try:
            wf_names = sorted(
                p.name for p in workflows_dir.iterdir() if p.is_file()
            )
        except OSError:
            wf_names = []
        if wf_names:
            _add(
                ".github/workflows (names only)",
                ".github/workflows/",
                "\n".join(wf_names) + "\n",
            )

    # 7. git context — branch, remote, last 20 commits, dirty file list.
    git_blocks: list[str] = []
    branch = _run_git(project_path, ["branch", "--show-current"])
    if branch is not None:
        git_blocks.append(f"current_branch: {branch.strip() or '(detached)'}")
    remote = _run_git(project_path, ["remote", "get-url", "origin"])
    if remote is not None:
        git_blocks.append(f"origin: {remote.strip()}")
    log = _run_git(project_path, ["log", "--oneline", "-20"])
    if log is not None:
        git_blocks.append("last_20_commits:\n" + log.rstrip() + "\n")
    dirty = _run_git(project_path, ["status", "--porcelain"])
    if dirty is not None:
        git_blocks.append(
            "dirty_files (porcelain):\n" + (dirty.rstrip() or "(clean)") + "\n"
        )
    if git_blocks:
        _add("git context", "git:meta", "\n".join(git_blocks) + "\n")

    # 8. Project size meta — for the LLM to gauge complexity.
    n_files = _file_count(project_path)
    _add(
        "project size",
        "(walk)",
        f"approx_file_count: {n_files}\n",
    )

    return bundle


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


def _load_schema_text() -> str:
    """Read the schema text from the first available candidate path."""
    for candidate in _SCHEMA_PATH_CANDIDATES:
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8")
            except OSError:
                continue
    raise FileNotFoundError(
        "AGENT.md schema not found in any known location: "
        + ", ".join(str(p) for p in _SCHEMA_PATH_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# LLM dispatch
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_PREFIX = """\
You are the AGENT.md drafter for the `homing` tool. Your job is to produce
one AGENT.md file (schema v2 — see below) for a single project, using ONLY
the deterministic input bundle the caller provides.

# Hard rules — these are load-bearing

1. Citations required. Every claim in the body MUST trace to a file in the
   input bundle. If a fact is not in the inputs, write
   `# TODO: <reason>` instead of guessing. Do not invent file paths,
   environment variable names, dependencies, or shell commands.

2. Source docs are authoritative. If the project has a CLAUDE.md /
   AGENTS.md / SPEC.md / README.md, treat them as ground truth. Quote or
   paraphrase faithfully — do not "improve" them, do not contradict them.

3. Schema fidelity. Output exactly one Markdown document with valid YAML
   frontmatter delimited by `---` lines, followed by a `# <Title>` and the
   standard body sections (`## What this is`, `## How to run`,
   `## Agent instructions`, `## Recent work`, `## Known issues`).
   Required frontmatter fields: name, purpose, state, type, stack.
   Use `# TODO: <reason>` whenever a field cannot be filled with confidence.

4. No marketing copy. Optimise the body for "fresh agent productive in 60
   seconds" — concrete commands, landmines, where state lives. Skip prose
   that doesn't help an agent resume work.

5. Density beats completeness. Keep the whole file under ~300 lines.

6. Record sources. Inside the frontmatter, populate
   `meta.sources` with the list of input file paths you actually read
   (use the `path` shown for each input in the bundle).

7. Output the AGENT.md document and nothing else. No prose before or after.
   No code fences around the document. Begin the response with `---` and
   end with the closing of the last body section.

# Schema (v2)

"""


_ASSISTANT_PRIMER = "---\n"


def _build_user_message(project_path: Path, inputs: list[_InputFile]) -> str:
    """Assemble the structured user message describing the project."""
    parts: list[str] = []
    parts.append(f"# Project under draft\n\nproject_path: {project_path}\n")
    parts.append(
        "# Input bundle\n"
        "Each section below is a separate file or context blob. Cite by the\n"
        "`path:` value shown.\n"
    )
    for item in inputs:
        parts.append(
            "\n---\n"
            f"label: {item.label}\n"
            f"path: {item.path}\n"
            "content:\n"
            "```\n"
            f"{item.content}"
            f"{'' if item.content.endswith(chr(10)) else chr(10)}"
            "```\n"
        )
    parts.append(
        "\n---\nReminder: cite every body claim by file path; use "
        "`# TODO: <reason>` if you cannot. Output the AGENT.md document only.\n"
    )
    return "".join(parts)


def _call_anthropic(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    user_message: str,
) -> tuple[str, int, int]:
    """Call the Anthropic API with prompt-cached system prompt.

    Returns ``(text, input_tokens, output_tokens)``. The system prompt is
    sent as a single block with ``cache_control`` ephemeral so repeated
    drafts within a 5-minute window reuse the cache.
    """
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": _ASSISTANT_PRIMER},
        ],
    )
    text_parts: list[str] = []
    for block in response.content:
        # SDK objects expose .text; dicts use ["text"].
        if hasattr(block, "text"):
            text_parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    body = "".join(text_parts)
    # The assistant primer was "---\n"; the model's continuation does not
    # include it, so prepend so we have the full document.
    if not body.lstrip().startswith("---"):
        body = _ASSISTANT_PRIMER + body
    usage = getattr(response, "usage", None)
    in_toks = getattr(usage, "input_tokens", 0) if usage is not None else 0
    out_toks = getattr(usage, "output_tokens", 0) if usage is not None else 0
    return body, int(in_toks), int(out_toks)


# ---------------------------------------------------------------------------
# Validation — deterministic, no LLM
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_FOREIGN_PATH_RE = re.compile(r"(?:^|\s)(?:/Users/|[A-Z]:\\)")


@dataclass(frozen=True)
class _ValidationOutcome:
    ok: bool
    reason: str | None
    frontmatter: dict[str, Any] | None
    body: str


def _validate_draft(content: str) -> _ValidationOutcome:
    """Parse + sanity-check an LLM-produced AGENT.md document."""
    m = _FRONTMATTER_RE.match(content)
    if m is None:
        return _ValidationOutcome(
            ok=False,
            reason="output is missing YAML frontmatter delimited by '---' lines",
            frontmatter=None,
            body="",
        )
    frontmatter_text = m.group(1)
    body_text = m.group(2)
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        return _ValidationOutcome(
            ok=False,
            reason=f"frontmatter YAML failed to parse: {e}",
            frontmatter=None,
            body=body_text,
        )
    if not isinstance(frontmatter, dict):
        return _ValidationOutcome(
            ok=False,
            reason="frontmatter must be a YAML mapping at the top level",
            frontmatter=None,
            body=body_text,
        )
    missing = [f for f in REQUIRED_FRONTMATTER_FIELDS if f not in frontmatter]
    if missing:
        return _ValidationOutcome(
            ok=False,
            reason=f"frontmatter is missing required fields: {missing}",
            frontmatter=frontmatter,
            body=body_text,
        )
    sections_present = sum(1 for s in STANDARD_BODY_SECTIONS if s in body_text)
    if sections_present < 3:
        return _ValidationOutcome(
            ok=False,
            reason=(
                f"body has only {sections_present} of the standard sections; "
                f"need at least 3 of {list(STANDARD_BODY_SECTIONS)}"
            ),
            frontmatter=frontmatter,
            body=body_text,
        )
    if _FOREIGN_PATH_RE.search(body_text):
        return _ValidationOutcome(
            ok=False,
            reason="body contains a foreign-OS path (/Users/... or X:\\...)",
            frontmatter=frontmatter,
            body=body_text,
        )
    return _ValidationOutcome(
        ok=True,
        reason=None,
        frontmatter=frontmatter,
        body=body_text,
    )


def _ensure_meta_sources(content: str, sources: list[str]) -> str:
    """Inject ``meta.sources`` into the frontmatter if missing.

    The schema requires the drafter to record its inputs. The model is
    instructed to do this; if it forgets, we add it deterministically so
    the citation evidence is always present on disk.
    """
    m = _FRONTMATTER_RE.match(content)
    if m is None:
        return content
    fm_text = m.group(1)
    body = m.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return content
    if not isinstance(fm, dict):
        return content
    meta = fm.get("meta") if isinstance(fm.get("meta"), dict) else {}
    existing = meta.get("sources") if isinstance(meta, dict) else None
    if isinstance(existing, list) and existing:
        # The model already populated sources — leave alone.
        return content
    meta = dict(meta) if isinstance(meta, dict) else {}
    meta["sources"] = sources
    fm["meta"] = meta
    new_fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{new_fm_text}\n---\n{body}"


# ---------------------------------------------------------------------------
# Conflict policy + write
# ---------------------------------------------------------------------------


def _resolve_output_path(output_path: Path, policy: str) -> Path:
    """Apply the no-silent-overwrite policy from CLAUDE.md.

    - ``proposed``: if file exists, return ``*.proposed.md`` sibling.
    - ``skip`` / ``fail``: caller handles before we get here.
    - default: return ``output_path`` unchanged when it does not exist.
    """
    if not output_path.exists():
        return output_path
    if policy == "proposed":
        if output_path.name.endswith(".md"):
            new_name = output_path.name[: -len(".md")] + ".proposed.md"
        else:
            new_name = output_path.name + ".proposed"
        return output_path.parent / new_name
    raise FileExistsError(f"refusing to overwrite existing manifest: {output_path}")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def _build_anthropic_client() -> Any:
    """Construct a default Anthropic client. Imported lazily for tests."""
    from anthropic import Anthropic  # type: ignore[import-not-found]

    return Anthropic()


def draft_agent_md(
    project_path: Path,
    output_path: Path,
    *,
    model: str = "claude-sonnet-4-6",
    overwrite_policy: str = "proposed",
    max_input_files: int = 25,
    max_input_tokens: int = 50_000,
    client: Any | None = None,
) -> DraftResult:
    """Generate an AGENT.md for ``project_path`` and write to ``output_path``.

    See module docstring for the high-level pipeline. ``client`` is an
    injection seam used by tests; production calls leave it ``None`` and
    a default ``anthropic.Anthropic()`` is constructed.
    """
    project_path = Path(project_path).resolve()
    output_path = Path(output_path)

    if overwrite_policy not in VALID_POLICIES:
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason=f"unknown overwrite_policy {overwrite_policy!r}",
            model=model,
        )

    if not project_path.is_dir():
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason=f"project_path is not a directory: {project_path}",
            model=model,
        )

    # --- conflict policy gate (BEFORE the LLM call) -----------------------
    if output_path.exists():
        if overwrite_policy == "skip":
            return DraftResult(
                project_path=project_path,
                output_path=output_path,
                status="skipped",
                reason="output already exists",
                model=model,
            )
        if overwrite_policy == "fail":
            raise FileExistsError(
                f"refusing to overwrite existing manifest: {output_path}"
            )
        # policy == "proposed" — fall through, will write to .proposed.md.

    # --- pre-process inputs ----------------------------------------------
    inputs = collect_inputs(
        project_path,
        max_input_files=max_input_files,
        max_input_tokens=max_input_tokens,
    )
    input_paths = [item.path for item in inputs]

    if not inputs:
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason="no input files collected from project",
            input_files_used=input_paths,
            model=model,
        )

    # --- build prompts ----------------------------------------------------
    try:
        schema_text = _load_schema_text()
    except FileNotFoundError as e:
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason=str(e),
            input_files_used=input_paths,
            model=model,
        )
    system_prompt = _SYSTEM_PROMPT_PREFIX + schema_text
    user_message = _build_user_message(project_path, inputs)

    # --- LLM call ---------------------------------------------------------
    if client is None:
        try:
            client = _build_anthropic_client()
        except Exception as e:  # noqa: BLE001 — surface SDK or env issues
            return DraftResult(
                project_path=project_path,
                output_path=None,
                status="failed",
                reason=f"failed to construct Anthropic client: {e}",
                input_files_used=input_paths,
                model=model,
            )

    try:
        body, in_toks, out_toks = _call_anthropic(
            client=client,
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
        )
    except Exception as e:  # noqa: BLE001 — anthropic raises a SDK error tree
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason=f"LLM call failed: {e}",
            input_files_used=input_paths,
            model=model,
        )

    # --- validate ---------------------------------------------------------
    outcome = _validate_draft(body)
    if not outcome.ok:
        return DraftResult(
            project_path=project_path,
            output_path=None,
            status="failed",
            reason=f"validation failed: {outcome.reason}",
            input_files_used=input_paths,
            tokens_input=in_toks,
            tokens_output=out_toks,
            model=model,
        )

    # --- inject meta.sources if model omitted it --------------------------
    body = _ensure_meta_sources(body, input_paths)

    # --- write ------------------------------------------------------------
    final_path = _resolve_output_path(output_path, overwrite_policy)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(body, encoding="utf-8")

    status = "proposed" if final_path != output_path else "drafted"
    return DraftResult(
        project_path=project_path,
        output_path=final_path,
        status=status,
        reason=None,
        input_files_used=input_paths,
        tokens_input=in_toks,
        tokens_output=out_toks,
        model=model,
    )

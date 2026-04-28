# CLAUDE.md — homing

Agent contract for **working on the `homing` repository itself** — i.e. you are an agent helping the user develop, test, or change `homing`'s code.

> **If you are an agent running migration work for a user (clone + claude pattern), this is not your file. Read [BOOTSTRAP.md](./BOOTSTRAP.md) instead.** That doc walks through the four modes (leaving / arriving / maintenance / exploring) and tells you what to do.

The rest of this file is about contributing to the codebase.

## Commands

```bash
# Setup
pip install -e ".[dev]"     # editable install with dev extras (pytest, ruff, black, mypy)

# Tests
pytest tests/               # full suite (~256 tests)
pytest tests/ -x            # stop on first failure
pytest --cov=src --cov-report=term-missing tests/  # with coverage

# Lint / format
ruff check src/ tests/      # linter
black --check src/ tests/   # formatter check
mypy src/                   # type check

# CLIs (after install)
homing --help
cabinet --help
```

## What this repo is

A tool that makes a personal laptop **legible to agents**. It walks `$HOME`, classifies what it finds, and produces a parallel structured representation (`~/system/`) that any agent can read to understand the machine without re-discovering everything. Migration is one downstream use case; daily agent productivity is another.

The tool itself is portable. The output is per-machine.

## Hard rules

These are non-negotiable. Violating them breaks the trust model the whole project rests on.

### 1. Source is sacred

The tool reads from `$HOME` (or wherever the user points it). It writes only to `~/system/` (or a configured output dir). It NEVER modifies, deletes, or moves anything in the source tree. Not `node_modules`, not `.cache`, not anything.

If a future feature appears to need source-side writes (e.g. "auto-fix .gitignore"), it goes through a separate, opt-in command with explicit confirmation per file. Even then, never as part of `enumerate` / `classify` / `draft` / `index`.

### 2. No silent destruction of existing manifests

Before writing any AGENT.md or PLACE.md to `~/system/projects/<name>/`:

- If the target file does not exist → write directly.
- If the target file **does** exist → write to `AGENT.proposed.md` (or `PLACE.proposed.md`) alongside it. NEVER overwrite the existing file. The user runs `homing reconcile` as an explicit merge step.

This applies even on `--force`. There is no flag that overwrites an existing manifest.

### 3. Existing project docs at the source are authoritative input, not competition

If the source project has `CLAUDE.md`, `README.md`, `SPEC.md`, `AGENTS.md`, `.cursorrules`, `.windsurfrules`, or similar agent-facing docs:

- The drafter MUST read them and treat them as ground truth.
- The drafter MUST cite them in the AGENT.md body when claims derive from them.
- The drafter MUST NOT try to replace them, summarize them in a way that supersedes them, or claim authority over them.

AGENT.md is a *derived view* over these source docs plus the filesystem. Source docs are upstream; AGENT.md is downstream.

### 4. Citations required

Every claim in an AGENT.md or PLACE.md body must trace to a file the drafter read. The drafter records its sources in `meta.sources` (frontmatter) and inline references where possible.

The validator agent rejects manifests with uncited claims. This is the load-bearing constraint that prevents hallucination.

### 5. Idempotent everywhere

Running `homing enumerate` twice on unchanged input produces byte-identical output (modulo timestamps). Same for `homing index`. Sub-commands declare their inputs explicitly so deterministic runs are testable.

### 6. Read-only against config when in doubt

Plugin rules, platform configs, schema files: tool reads these. It never modifies them as a side effect of normal operation. Schema migrations are explicit, versioned, and human-confirmed.

## Architecture in one paragraph

`homing` is a static-analysis-shaped pipeline. Phase A (`enumerate`) does a deterministic two-pass walk of `$HOME` (project hunter + place classifier) using per-platform YAML config. Phase B (`summary`) emits a deterministic 5-minute-read overview — runs without an LLM. Phase C (`rules`) runs deterministic rule plugins over each unit; most units classify here without touching the LLM. Phase D (`classify`) invokes the LLM only on units with low rule confidence. Phase E (`draft`) generates AGENT.md / PLACE.md for confirmed units, with citation enforcement. Phase F (`validate`) runs a fresh-agent test against each manifest. Phase G (`index`) aggregates frontmatter into a queryable JSON. State across phases lives in a SQLite worklist for resume-ability.

## Layout

```
src/homing/
├── enumerate.py       Phase A — two-pass walk
├── summary.py         Phase B — deterministic overview
├── rules/             Phase C — pluggable, runtime-discovered
│   ├── __init__.py
│   ├── base.py        Rule base class + registration
│   └── *.py           Each file = one rule
├── classify.py        Phase D — LLM dispatch (currently a stub; the working
│                      path is `--via-orchestrator` which writes request bundles
│                      to `~/system/draft-requests/` for the Claude Code session
│                      to fan out subagents)
├── draft.py           Phase E — AGENT.md / PLACE.md generation
├── draft_cli.py       Phase E CLI shim — exposes `homing draft` + `--via-orchestrator`
├── validate.py        Phase F — fresh-agent test runner
├── index.py           Phase G — frontmatter aggregation
├── worklist.py        SQLite-backed state across phases
├── orchestrator.py    Wave-based parallel dispatch
├── platform.py        Loads config/platforms/<os>.yaml
└── cli.py             `homing` typer app

src/cabinet/
├── scan.py            Phase A — homogeneity scan over a folder tree
├── classify.py        Phase B — deterministic + LLM classification (also has
│                      `--via-orchestrator` which emits `~/cabinet/batches/`)
├── triage.py          Phase C — write triage.md for the user to mark up
├── reconcile.py       Phase D — read user's marked triage.md back into decisions
├── planner.py         Phase E — build an ActionPlan from approved decisions
├── undo.py            Phase F — apply with undo ledger; the chaos-test target
├── worklist.py        SQLite worklist (separate schema from homing's)
└── cli.py             `cabinet` typer app
```

## Output contract

The tool writes to `~/system/` (configurable). Layout:

```
~/system/
├── overview.md                       # Phase B output
├── enumeration.json                  # Phase A output
├── worklist.sqlite                   # cross-phase state
├── index.json                        # Phase G output
├── projects/<name>/AGENT.md          # rich manifests for projects
├── projects/<name>/AGENT.proposed.md # if a manifest already exists
├── places/<name>/PLACE.md            # narrower manifests for non-projects
└── meta/
    ├── runs/<timestamp>.log
    └── unresolved.md                 # human-review queue
```

## Plugin discovery (rules)

Rules live in `src/homing/rules/*.py`. Each file defines one or more `Rule` subclasses. The registry walks the package at runtime and collects them. Adding a rule means dropping a new `.py` file — no manual registration list.

Each rule must implement:

```python
class Rule:
    name: str                                  # unique kebab-case
    requires: list[str] = []                   # other rule names that must run first
    def applies(self, unit: UnitSummary) -> bool: ...
    def evaluate(self, unit: UnitSummary) -> RuleFinding: ...
```

A `RuleFinding` carries:
- `confidence`: 0.0–1.0
- `classifications`: dict (e.g. `{"type": "node-project", "state": "active"}`)
- `evidence`: list of `(path, reason)` tuples — used as citations when LLM later writes the body

## When to use the LLM

Default: don't. Deterministic rules cover most $HOME content (caches, toolchains, vendored installs, things with `.git`). The LLM fires only when:

1. **Multiple rules disagree** on classification → mediator role
2. **No rule matched** → unknown directory, ask the LLM what it sees
3. **Drafting AGENT.md / PLACE.md body** → the rich human-language sections
4. **Validation** → fresh-agent test reads only the manifest

Model selection: Sonnet by default. Opus for low-confidence drafting on complex projects (>1000 files, >2 detected stacks, or rule confidence <0.5). Haiku reserved for batch validation passes if cost matters.

## Agent contract on this codebase

When working on `homing` itself:

- Run `pytest` before claiming a feature done.
- New rules must come with tests in `tests/rules/test_<rule_name>.py` against fixture trees in `tests/fixtures/`.
- New platform support means adding `config/platforms/<os>.yaml` + adding the platform to `tests/test_platform.py`.
- Schema changes bump the `schema_version` field; the indexer must handle a mixed-version index gracefully (warn on mismatch, don't crash).
- Citations apply to the codebase too: don't claim a function exists that you didn't grep for.

## Testing approach

- Unit: each rule against fixture trees.
- Integration: full `enumerate` → `summary` → `rules` → `index` against a fixture `$HOME`.
- E2E: dry-run against the real machine in a sandbox writing to `/tmp/system-test/`.
- LLM-touching code is mocked in tests; the real LLM is exercised by an opt-in `tests/live/` suite.

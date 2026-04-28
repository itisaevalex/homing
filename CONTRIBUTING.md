# Contributing to homing

Thank you for your interest in contributing. This document covers the local development setup, test workflow, code style, and PR expectations.

For architecture, hard rules, and agent contract details, see [CLAUDE.md](./CLAUDE.md) — that file is the primary contributor contract and is more detailed than this one.

## Local dev setup

```bash
git clone https://github.com/itisaevalex/homing.git
cd homing
pip install -e ".[dev]"
```

This gives you an editable install of both `homing` and `cabinet` CLIs, plus all dev dependencies (`pytest`, `pytest-cov`, `ruff`, `black`, `mypy`).

If you also want to exercise `bootstrap.sh` (chezmoi + age install + migrate skill):

```bash
./bootstrap.sh
```

`bootstrap.sh` is idempotent; it skips anything already installed.

## Running tests

```bash
pytest tests/                      # full suite (~256 tests)
pytest tests/ -x                   # stop on first failure
pytest --cov=src --cov-report=term-missing tests/   # coverage report
```

All 256 tests must pass before a PR is merged. Coverage must stay at or above 80%.

LLM-touching code is mocked in unit tests. There is an opt-in live suite:

```bash
pytest tests/live/                 # requires ANTHROPIC_API_KEY; do not run in CI
```

## Code style

| Tool | Config |
|------|--------|
| Formatter | `black` — 100-char lines, target Python 3.11 |
| Linter | `ruff` — rules E, F, I, B, UP, SIM, TID |
| Type checker | `mypy` — strict mode |

Quick check before committing:

```bash
ruff check src/ tests/
black --check src/ tests/
mypy src/
```

Ruff and black are the arbiters. If they agree, so do we. Do not introduce `# noqa` suppressions without a comment explaining why.

## Hard rules (from CLAUDE.md)

These apply to code contributions as much as to agent behaviour:

1. **Source is sacred.** No command may modify, delete, or move anything under `$HOME` as a side effect of enumeration, classification, or drafting. Any future write-back feature must be opt-in, per-file confirmed, and live behind a separate subcommand.

2. **No silent overwrites.** `homing` never overwrites an existing manifest. Updates go to `*.proposed.md`. There is no `--force` that bypasses this.

3. **Existing project docs are authoritative input.** When the drafter reads a `CLAUDE.md`, `README.md`, or similar, it cites them — it does not supersede them.

4. **Citations required.** Every claim in a generated AGENT.md or PLACE.md must trace to a file the drafter read. The validator rejects manifests with uncited claims. This rule applies to the codebase too: do not assert that a function exists without having grepped for it.

5. **Idempotent everywhere.** Running any command twice on unchanged input must produce byte-identical output (modulo timestamps). New phases must declare their inputs explicitly.

6. **Read-only against config.** Plugin rules and platform YAML files are read-only during normal operation. Schema migrations are explicit and human-confirmed.

## Adding a new rule plugin

1. Drop a new file `src/homing/rules/<kebab_name>.py`.
2. Define one or more `Rule` subclasses (see `src/homing/rules/base.py`).
3. The registry auto-discovers it at runtime — no manual registration.
4. Add tests in `tests/rules/test_<rule_name>.py` using fixture directory trees under `tests/fixtures/`.
5. Run `pytest tests/` — all existing tests must still pass.

## Adding platform support

1. Create `config/platforms/<os>.yaml` following the schema in `config/platforms/linux.yaml`.
2. Add the platform to `tests/test_platform.py`.
3. Update the "Platforms" note in `CLAUDE.md` if the platform reaches parity.

## PR expectations

- All tests pass (`pytest tests/`).
- `ruff check` and `black --check` are clean.
- `mypy src/` is clean (or new errors are justified with inline comments).
- New functionality has tests. Aim for 80%+ coverage on changed files.
- If your change touches AGENT.md / PLACE.md generation, the citation rule applies: the drafter must cite what it read.
- Commit messages follow conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- One logical change per PR. Large refactors should be discussed in an issue first.

## Reporting issues

See the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include:
- Which mode (A/B/C/D) you were running.
- OS and Python version.
- Output of `homing --version` or `cabinet --version`.
- Which step of `bootstrap.sh` failed, if applicable.
- The full error output.

## Using Claude Code

This project includes a `CLAUDE.md` that Claude Code reads automatically when you open the repo.

```bash
claude    # in the homing repo directory
```

The agent has full context on the architecture, hard rules, and test commands. For migration workflows (not contributor work), see `BOOTSTRAP.md`.

```
╭──────────────────────────────────────────────────────────────────────╮
│                                                                      │
│   ██╗  ██╗  ██████╗  ███╗   ███╗ ██╗ ███╗   ██╗  ██████╗             │
│   ██║  ██║ ██╔═══██╗ ████╗ ████║ ██║ ████╗  ██║ ██╔════╝             │
│   ███████║ ██║   ██║ ██╔████╔██║ ██║ ██╔██╗ ██║ ██║  ███╗            │
│   ██╔══██║ ██║   ██║ ██║╚██╔╝██║ ██║ ██║╚██╗██║ ██║   ██║            │
│   ██║  ██║ ╚██████╔╝ ██║ ╚═╝ ██║ ██║ ██║ ╚████║ ╚██████╔╝            │
│   ╚═╝  ╚═╝  ╚═════╝  ╚═╝     ╚═╝ ╚═╝ ╚═╝  ╚═══╝  ╚═════╝             │
│                                                                      │
│   ┌─[old laptop]──╮  pack  ╭──[ usb ]──╮  apply  ╭──[new laptop]─┐   │
│   │  $HOME · agent│ ━━━━━▶ │ encrypted │ ━━━━━━▶ │  $HOME · agent│   │
│   └───────────────╯ ◀─audit│  bundle   │         └───────────────┘   │
│                            ╰───────────╯                             │
│                                                                      │
│         laptop migration · agent legibility · doc triage             │
│                                                                      │
╰──────────────────────────────────────────────────────────────────────╯
```

# homing

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](INTENT.md)

> An agent harness for laptop migration, dev-environment legibility, and personal-document triage.

`homing` ships two CLI tools that share a substrate:

1. **`homing`** — walks `$HOME`, classifies dev projects, produces a structured `~/system/` representation.
2. **`cabinet`** — walks personal-document folders (`~/Documents`, `~/Pictures`, `~/Downloads`), triages messy piles into reversible move/archive/keep decisions.

Both are driven by the orchestrating Claude Code session via the `migrate` skill (auto-installed by `bootstrap.sh`). LLM-tier classification runs through Claude Code subagents — no `ANTHROPIC_API_KEY` needed when used inside Claude Code.

The tool is portable across Linux, macOS, Windows. The output is per-machine. The bundle is physical-transfer only (USB stick, age-encrypted secrets) by design — nothing about your personal config layer goes through a network.

## Quickstart for the agent harness use case

```bash
# on any machine — old or new
git clone https://github.com/itisaevalex/homing.git
cd homing
./bootstrap.sh    # install deps + migrate skill (idempotent)
claude            # start your agent here
```

The agent reads `BOOTSTRAP.md` automatically and asks which mode you're in:

- **A — Leaving** this machine: generate a bundle to take to the next one.
- **B — Arriving** at this machine: apply a bundle that's on a USB.
- **C — Maintenance**: refresh the existing system on this machine (re-run rules, draft missing AGENT.md, re-triage a folder). No migration.
- **D — Just exploring**: map the laptop with no migration and no manifest churn.

### Inside Claude Code? You don't need an API key.

`homing` and `cabinet` are designed to run inside a Claude Code session. When you start `claude` in this repo, the `migrate` skill (installed by `bootstrap.sh` into `~/.claude/skills/`) is auto-discovered. LLM classification happens through Claude Code's session auth — no `ANTHROPIC_API_KEY` required. See [`BOOTSTRAP.md`](./BOOTSTRAP.md) for the full agent entry doc and [`skills/migrate/SKILL.md`](./skills/migrate/SKILL.md) for the detailed per-mode playbooks.

## Why

Machines accumulate. Years of projects, half-finished spikes, vendored installs, downloaded model checkpoints, six layers of caches. Built-in OS migration tools copy this stuff blindly. `homing` *classifies* it: what's canonical, what's regenerable, what's dead, what's alive, what's actually mine vs. just sitting here.

The output is a structured, queryable view of your machine that:

- Makes any agent productive on any project in 60 seconds.
- Makes migration a calculated decision instead of a panic copy.
- Surfaces the things you'd forgotten about (which is most of them).
- Lives alongside your filesystem, not in place of it. Your `$HOME` is untouched.

## Status

v0.1 in active development. Not for general use yet. See [`INTENT.md`](./INTENT.md) for the roadmap.

## Prerequisites

- Python 3.11+
- `pip` (or `python3 -m pip`)
- `curl` (for `bootstrap.sh` to install chezmoi and age)
- Optional: `apt` or `brew` for `poppler-utils` (cabinet PDF rendering)

## Install

```bash
git clone https://github.com/itisaevalex/homing.git
cd homing
./bootstrap.sh
```

`bootstrap.sh` is idempotent. It installs chezmoi, age (sha256-pinned), the `homing` and `cabinet` CLIs via `pip install -e .`, and the `migrate` skill into `~/.claude/skills/`. If `homing` is not on PATH after bootstrap, run `export PATH="$HOME/.local/bin:$PATH"`.

For development (tests, linting):

```bash
pip install -e ".[dev]"
```

## Usage

```bash
homing enumerate                   # walk $HOME, classify into a worklist
homing summary                     # 5-minute readable overview, no LLM
homing rules                       # deterministic classification (most units done here)
homing classify --via-orchestrator # LLM only on the leftovers (uses Claude Code subagents)
homing draft --batch               # generate AGENT.md / PLACE.md for confirmed units
homing validate --all              # fresh-agent test against every manifest
homing index                       # aggregate frontmatter → index.json
homing query active                # list active projects
homing query show <name>           # full record + AGENT.md path
homing query stale                 # last_meaningful_activity > 90d
```

## Output

Everything goes to `~/system/` (configurable):

```
~/system/
├── overview.md            # the readable summary
├── enumeration.json       # full unit list
├── index.json             # aggregated frontmatter
├── projects/<name>/AGENT.md
├── places/<name>/PLACE.md
└── worklist.sqlite        # resume-able state
```

## Hard rules

- **Source is sacred.** Never modifies your `$HOME`.
- **No silent overwrites.** Existing manifests are protected; updates go to `*.proposed.md`.
- **Citations required.** Every claim in a manifest body traces to a file the drafter actually read.
- **Idempotent.** Running twice on unchanged input = identical output.

See [`CLAUDE.md`](./CLAUDE.md) for the full agent contract.

## Using with Claude Code

This project includes a [`CLAUDE.md`](./CLAUDE.md) contributor contract and a [`BOOTSTRAP.md`](./BOOTSTRAP.md) agent entry doc that Claude Code reads automatically.

```bash
./bootstrap.sh   # one-time: installs migrate skill into ~/.claude/skills/
claude           # Claude Code reads CLAUDE.md + BOOTSTRAP.md + the migrate skill
```

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Design

See [`INTENT.md`](./INTENT.md) for what success looks like and what's explicitly out of scope.

## License

MIT — see [LICENSE](./LICENSE).

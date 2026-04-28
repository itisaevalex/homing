# homing

> An agent harness for laptop migration, dev-environment legibility, and personal-document triage.

`homing` ships two CLI tools that share a substrate:

1. **`homing`** — walks `$HOME`, classifies dev projects, produces a structured `~/system/` representation.
2. **`cabinet`** — walks personal-document folders (`~/Documents`, `~/Pictures`, `~/Downloads`), triages messy piles into reversible move/archive/keep decisions.

Both are driven by the orchestrating Claude Code session via the `migrate` skill (auto-installed by `bootstrap.sh`). LLM-tier classification runs through Claude Code subagents — no `ANTHROPIC_API_KEY` needed when used inside Claude Code.

The tool is portable across Linux, macOS, Windows. The output is per-machine. The bundle is physical-transfer only (USB stick, age-encrypted secrets) by design — nothing about your personal config layer goes through a network.

## Quickstart for the agent harness use case

```bash
# on any machine — old or new
git clone git@github.com:itisaevalex/homing.git
cd homing
claude    # start your agent here
```

The agent's first move should be to read `BOOTSTRAP.md` and ask which mode you're in:

- **A — Leaving** this machine: generate a bundle to take to the next one.
- **B — Arriving** at this machine: apply a bundle that's on a USB.
- **C — Just exploring**: map the current laptop, no migration.

## Why

Machines accumulate. Years of projects, half-finished spikes, vendored installs, downloaded model checkpoints, six layers of caches. Built-in OS migration tools copy this stuff blindly. `homing` *classifies* it: what's canonical, what's regenerable, what's dead, what's alive, what's actually mine vs. just sitting here.

The output is a structured, queryable view of your machine that:

- Makes any agent productive on any project in 60 seconds.
- Makes migration a calculated decision instead of a panic copy.
- Surfaces the things you'd forgotten about (which is most of them).
- Lives alongside your filesystem, not in place of it. Your `$HOME` is untouched.

## Status

v0.1 in active development. Not for general use yet.

## Install

```bash
git clone <repo-url>
cd homing
pip install -e .
```

## Usage (planned)

```bash
homing enumerate                  # walk $HOME, classify into a worklist
homing summary                    # 5-minute readable overview, no LLM
homing rules                      # deterministic classification (most units done here)
homing classify --remaining       # LLM only on the leftovers
homing draft --batch              # generate AGENT.md / PLACE.md for confirmed units
homing validate --all             # fresh-agent test against every manifest
homing index                      # aggregate frontmatter → index.json
homing query active               # list active projects
homing query show <name>          # full record + AGENT.md path
homing query stale                # last_meaningful_activity > 90d
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

## Design

See [`INTENT.md`](./INTENT.md) for what success looks like and what's explicitly out of scope.

## License

MIT.

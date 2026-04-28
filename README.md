# homing

> An agent that makes your laptop legible to other agents.

`homing` walks your `$HOME`, classifies what it finds, and produces a parallel structured representation that any agent (Claude Code, Hermes, whatever) can read to understand your machine in seconds — without re-discovering the layout, the active projects, the dead ones, or the conventions you use.

The tool is portable across Linux, macOS, Windows. The output is per-machine.

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

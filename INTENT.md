# INTENT.md — homing

## What success looks like

A fresh agent landing on a machine that has run `homing` should be productive in under 60 seconds, without having to re-discover the layout, conventions, or active work. It reads `~/system/index.json`, picks the relevant project, opens its `AGENT.md`, and starts working.

A user who has run `homing` should be able to answer, in seconds, questions they currently can't:

- *What's actually on my laptop?*
- *Which of my projects are alive vs. abandoned?*
- *What's regenerable vs. canonical?*
- *If I migrated tomorrow, what would I have to bring?*
- *What's the biggest thing I haven't touched in two years?*

## Concrete deliverables (v0.1)

1. `homing enumerate` produces `enumeration.json` covering the entire `$HOME` accessible to the user, classified into project / place / cache-skip / unclear.
2. `homing summary` produces `overview.md` — readable in 5 minutes, no LLM, single artifact that lets the user decide whether to continue.
3. `homing rules` runs deterministic rule plugins; >70% of units classified without LLM.
4. `homing classify --via-orchestrator` resolves the rest by emitting batches for the orchestrating Claude Code session to fan out subagents on (or `homing classify` standalone if `ANTHROPIC_API_KEY` is set).
5. `homing draft <name>` produces an AGENT.md or PLACE.md with citations.
6. `homing validate <name>` confirms the manifest passes a fresh-agent test (≥7/10 confidence).
7. `homing index` aggregates all manifests into `index.json`.
8. `homing query` answers "list active projects," "show <name>," "list stale," "list unresolved."

If all 8 work end-to-end on a Linux laptop within a 2-hour single run, v0.1 is shipped.

## Non-goals (v0.1)

These are explicitly out of scope. They are interesting, they will tempt scope-creep, and they wait.

- **Apply / write-back to source.** No command modifies the user's actual `$HOME`. Migration tooling is separate.
- **Knowledge graph / edge modeling.** Flat index until coverage is real. Graph is post-v0.2 if at all.
- **TUI or web dashboard.** CLI only.
- **Cross-machine sync of `~/system/`.** Per-machine output. Sync via the user's existing tools (chezmoi, restic, git) if at all.
- **Continuous file-watcher / live updates.** `homing` runs on demand. No daemons.
- **Auto-installation / package re-installation on a new machine.** That's a downstream tool that consumes `index.json`; not part of this repo.

## What this is not solving

- Backup. Use restic.
- Dotfile sync. Use chezmoi.
- Memory across sessions. Use Honcho.
- Long-running personal agent. Use Hermes.

`homing` makes the machine *legible*. The other tools act on the legibility.

## Validation: how we know it works

Three tests, in order of importance:

1. **Fresh-agent test** — a Claude Code session given only `~/system/projects/<name>/AGENT.md` can answer "what is this and how do I resume?" with self-rated confidence ≥7/10. If under 7/10 across most projects, the schema or the drafter is wrong.
2. **Cancel-on-summary test** — `homing summary` produces an `overview.md` that, on first read, surfaces at least one thing the user wasn't expecting. (If it doesn't, the summary is too generic.)
3. **Idempotency test** — running the full pipeline twice on an unchanged `$HOME` produces byte-identical artifacts (excluding timestamps). If not, something in the pipeline is non-deterministic and we trust it less.

## What changes after v0.1

After the first machine has been mapped, we'll know:
- Whether the rule taxonomy is sufficient or needs categories we didn't anticipate
- Whether the AGENT.md schema needs a third version
- Whether the LLM-fallback rate is too high (rules are too coarse) or too low (we're hallucinating without LLM)
- Whether the 2-hour budget holds at this $HOME size, and where the long poles are

Those answers shape v0.2 — which is probably "the migration consumer that reads `index.json` and produces a target-machine setup script." But we don't write a line of v0.2 until v0.1 is real on this laptop and on a Windows laptop.

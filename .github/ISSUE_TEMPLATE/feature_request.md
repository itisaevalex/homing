---
name: Feature request
about: Propose a new capability or change to an existing one
title: "[feature] "
labels: enhancement
assignees: ''
---

## Summary

One sentence: what would you like homing or cabinet to do that it doesn't do today?

## Which tool / area

- [ ] `homing` enumeration / classification / drafting / validation / index / query
- [ ] `cabinet` scan / classify / triage / reconcile / plan / apply / undo
- [ ] `bootstrap.sh` / install flow
- [ ] `bundle.sh` / migration bundle generation
- [ ] Rule plugins (`src/homing/rules/` or `src/cabinet/rules/`)
- [ ] Platform support (new `config/platforms/<os>.yaml`)
- [ ] `--via-orchestrator` fan-out pattern
- [ ] Output schema / AGENT.md / PLACE.md format
- [ ] Documentation

## Motivation

What problem does this solve, or what workflow does it enable? Include the mode (A/B/C/D) if relevant.

## Proposed behaviour

Describe what the feature would do. Include the command signature if you have one in mind.

```bash
# example proposed command
homing <new-subcommand> [options]
```

## Non-goals / out of scope

What should this feature explicitly NOT do? (Referencing [INTENT.md](../../INTENT.md) non-goals is helpful here.)

## Alternatives considered

What workarounds exist today, and why are they insufficient?

## Hard-rule compatibility check

Does this feature comply with homing's hard rules (from [CLAUDE.md](../../CLAUDE.md))?

- [ ] Source is sacred — the feature does not modify `$HOME` as a side effect of classification
- [ ] No silent overwrites — any write goes through the proposed / reconcile path
- [ ] Citations required — any generated content cites the files it read
- [ ] Idempotent — running twice on unchanged input produces identical output

If you had to check "no" on any of these, explain the trade-off you have in mind.

## Additional context

Screenshots, example output, links to related issues, or anything else that helps.

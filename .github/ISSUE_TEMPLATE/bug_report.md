---
name: Bug report
about: Something is broken or behaving unexpectedly
title: "[bug] "
labels: bug
assignees: ''
---

## Summary

A clear one-sentence description of what went wrong.

## Mode

Which mode were you running when the bug occurred?

- [ ] A — Leaving (generating a bundle)
- [ ] B — Arriving (applying a bundle)
- [ ] C — Maintenance (re-scan, re-triage, drift detection)
- [ ] D — Just exploring (no migration, read-only)
- [ ] Contributing / running tests (not a migration workflow)
- [ ] N/A — bug is in `cabinet` triage, not a mode-specific flow

## Which tool / command

```
# Paste the exact command you ran, e.g.:
homing enumerate
cabinet scan ~/Documents --output-dir ~/cabinet
./bootstrap.sh
```

## Expected behaviour

What did you expect to happen?

## Actual behaviour

What happened instead? Paste the full terminal output, including any error tracebacks.

```
# paste output here
```

## Environment

| Field | Value |
|-------|-------|
| OS and version | e.g. Ubuntu 24.04, macOS 14.5, Windows 11 |
| Python version | e.g. 3.11.9 |
| `homing --version` | |
| `cabinet --version` | |
| Install method | `./bootstrap.sh` / `pip install -e .` / other |

## bootstrap.sh step that failed (if applicable)

If `bootstrap.sh` failed, which step number did it fail at?

- [ ] [1/5] chezmoi
- [ ] [2/5] age
- [ ] [3/5] homing python package
- [ ] [4/5] poppler-utils
- [ ] [5/5] migrate skill install
- [ ] Verify step at the end
- [ ] N/A

## Relevant files

Did the bug corrupt or fail to produce any output files? List them:

- `~/system/` state (does the directory exist?):
- `~/cabinet/` state (if cabinet was involved):
- Any `*.proposed.md` or `*.sqlite` files that look wrong:

## Additional context

Any other context that might help — config changes, unusual `$HOME` layout, custom `CABINET_TAXONOMY_PATH`, etc.

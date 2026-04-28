#!/usr/bin/env bash
# setup.sh — first-time setup for homing.
#
# Delegates to bootstrap.sh, which is idempotent and handles everything:
#   - chezmoi install
#   - age install (sha256-pinned)
#   - pip install -e . (homing + cabinet CLIs)
#   - poppler-utils for cabinet's PDF rendering (best-effort)
#   - migrate skill install into ~/.claude/skills/
#
# Usage: ./setup.sh
exec "$(dirname "${BASH_SOURCE[0]}")/bootstrap.sh" "$@"

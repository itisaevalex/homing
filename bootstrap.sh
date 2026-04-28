#!/usr/bin/env bash
# bootstrap.sh — install homing's runtime dependencies (chezmoi, age, python pkg).
#
# Idempotent. Safe to run multiple times. Never sudo unless absolutely required
# (and even then, only with the user's explicit consent — this script never
# invokes sudo without a prompt).
#
# Usage:
#   ./bootstrap.sh
#
# Exits non-zero if any required dep can't be installed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

echo "=== homing bootstrap ==="
echo "Repo: $REPO_ROOT"
echo "Bin target: $HOME/.local/bin"
echo ""

# ---------------------------------------------------------------------------
# 1. chezmoi — manages user dotfiles
# ---------------------------------------------------------------------------
if command -v chezmoi >/dev/null 2>&1; then
  echo "[1/3] chezmoi: already installed ($(chezmoi --version | head -1))"
else
  echo "[1/3] installing chezmoi to $HOME/.local/bin..."
  sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin" >/dev/null 2>&1
  command -v chezmoi >/dev/null || { echo "FAILED: chezmoi install"; exit 1; }
  echo "      installed: $(chezmoi --version | head -1)"
fi

# ---------------------------------------------------------------------------
# 2. age — encrypts the secrets bundle
# ---------------------------------------------------------------------------
AGE_VERSION="v1.2.1"
if command -v age >/dev/null 2>&1; then
  echo "[2/3] age: already installed ($(age --version))"
else
  echo "[2/3] installing age $AGE_VERSION to $HOME/.local/bin..."
  AGE_TGZ=$(mktemp --suffix=.tar.gz)
  curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz" \
    -o "$AGE_TGZ"
  AGE_DIR=$(mktemp -d)
  tar xzf "$AGE_TGZ" -C "$AGE_DIR"
  mv "$AGE_DIR/age/age" "$AGE_DIR/age/age-keygen" "$HOME/.local/bin/"
  chmod +x "$HOME/.local/bin/age" "$HOME/.local/bin/age-keygen"
  rm -rf "$AGE_DIR" "$AGE_TGZ"
  echo "      installed: $(age --version)"
fi

# ---------------------------------------------------------------------------
# 3. homing python package — editable install
# ---------------------------------------------------------------------------
if command -v homing >/dev/null 2>&1; then
  echo "[3/3] homing CLI: already installed ($(homing --version 2>&1 | tail -1))"
else
  echo "[3/3] installing homing python package (editable)..."
  if ! command -v pip >/dev/null 2>&1; then
    echo "      pip not found. Trying python3 -m pip..."
    if ! python3 -m pip --version >/dev/null 2>&1; then
      echo ""
      echo "      ERROR: neither 'pip' nor 'python3 -m pip' is available."
      echo "      Install pip via your distro: sudo apt install python3-pip (Ubuntu/Debian)"
      echo "      then re-run ./bootstrap.sh"
      exit 1
    fi
    PIP="python3 -m pip"
  else
    PIP="pip"
  fi
  $PIP install --user -e "$REPO_ROOT" >/dev/null
  command -v homing >/dev/null || {
    echo "      WARNING: 'homing' not on PATH. Add ~/.local/bin to PATH or restart shell."
  }
  echo "      installed (editable from $REPO_ROOT)"
fi

# ---------------------------------------------------------------------------
# 4. install the migrate skill into ~/.claude/skills/ so any Claude Code
#    session on this machine has the orchestrator playbook available.
# ---------------------------------------------------------------------------
SKILL_SRC="${REPO_ROOT}/skills/migrate/SKILL.md"
SKILL_DEST_DIR="${HOME}/.claude/skills/migrate"
if [ -f "$SKILL_SRC" ]; then
  mkdir -p "$SKILL_DEST_DIR"
  if [ ! -e "$SKILL_DEST_DIR/SKILL.md" ] || ! cmp -s "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"; then
    cp "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"
    echo "[4/4] migrate skill: installed to $SKILL_DEST_DIR"
  else
    echo "[4/4] migrate skill: already up to date at $SKILL_DEST_DIR"
  fi
else
  echo "[4/4] migrate skill: source not found at $SKILL_SRC (skipping)"
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
echo ""
echo "=== verify ==="
chezmoi --version | head -1
age --version
python3 -c "import homing; print(f'homing v{homing.__version__} from {homing.__file__}')"
[ -f "$SKILL_DEST_DIR/SKILL.md" ] && echo "migrate skill: $SKILL_DEST_DIR/SKILL.md"

echo ""
echo "=== done ==="
echo "Next: in a fresh Claude Code session, the migrate skill will be auto-discovered."
echo "Read BOOTSTRAP.md, then ask the user which mode (A/B/C) they're in."
echo "PATH addition: export PATH=\"\$HOME/.local/bin:\$PATH\" if 'homing' isn't found in a new shell."

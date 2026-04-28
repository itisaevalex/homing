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
  echo "[1/5] chezmoi: already installed ($(chezmoi --version | head -1))"
else
  echo "[1/5] installing chezmoi to $HOME/.local/bin..."
  sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin" >/dev/null 2>&1
  command -v chezmoi >/dev/null || { echo "FAILED: chezmoi install"; exit 1; }
  echo "      installed: $(chezmoi --version | head -1)"
fi

# ---------------------------------------------------------------------------
# 2. age — encrypts the secrets bundle
# ---------------------------------------------------------------------------
# Pin both version and sha256 of the linux-amd64 tarball. If FiloSottile
# updates a release in place, the integrity check fails loudly rather than
# silently installing a different binary.
AGE_VERSION="v1.2.1"
AGE_SHA256_LINUX_AMD64="7df45a6cc87d4da11cc03a539a7470c15b1041ab2b396af088fe9990f7c79d50"

if command -v age >/dev/null 2>&1; then
  echo "[2/5] age: already installed ($(age --version))"
else
  echo "[2/5] installing age $AGE_VERSION to $HOME/.local/bin..."
  AGE_TGZ=$(mktemp --suffix=.tar.gz)
  AGE_DIR=$(mktemp -d)
  trap 'rm -rf "$AGE_DIR" "$AGE_TGZ"' EXIT
  curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz" \
    -o "$AGE_TGZ"
  if command -v sha256sum >/dev/null 2>&1; then
    GOT=$(sha256sum "$AGE_TGZ" | awk '{print $1}')
    if [ "$GOT" != "$AGE_SHA256_LINUX_AMD64" ]; then
      echo "      ERROR: age tarball sha256 mismatch" >&2
      echo "      expected: $AGE_SHA256_LINUX_AMD64" >&2
      echo "      got:      $GOT" >&2
      exit 1
    fi
  else
    echo "      WARNING: sha256sum not present; skipping age tarball integrity check"
  fi
  tar xzf "$AGE_TGZ" -C "$AGE_DIR"
  mv "$AGE_DIR/age/age" "$AGE_DIR/age/age-keygen" "$HOME/.local/bin/"
  chmod +x "$HOME/.local/bin/age" "$HOME/.local/bin/age-keygen"
  trap - EXIT
  rm -rf "$AGE_DIR" "$AGE_TGZ"
  echo "      installed: $(age --version)"
fi

# ---------------------------------------------------------------------------
# 3. homing python package — editable install
# ---------------------------------------------------------------------------
if command -v homing >/dev/null 2>&1; then
  echo "[3/5] homing CLI: already installed"
else
  echo "[3/5] installing homing python package (editable)..."
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
# 4. poppler-utils — required by pdf2image for cabinet's PDF first-page render.
#    Best-effort: only installs if apt is present; doesn't fail otherwise.
# ---------------------------------------------------------------------------
if command -v pdftoppm >/dev/null 2>&1; then
  echo "[4/5] poppler-utils: already present ($(pdftoppm -v 2>&1 | head -1))"
elif command -v apt-get >/dev/null 2>&1; then
  echo "[4/5] installing poppler-utils via apt (requires sudo, prompts once)..."
  if sudo -n apt-get install -y poppler-utils >/dev/null 2>&1 || sudo apt-get install -y poppler-utils >/dev/null 2>&1; then
    echo "      installed"
  else
    echo "      WARNING: poppler-utils not installed (cabinet's PDF render will fail). Install manually: sudo apt install poppler-utils"
  fi
elif command -v brew >/dev/null 2>&1; then
  brew install poppler 2>&1 | tail -1
else
  echo "[4/5] poppler-utils: not present and no apt/brew detected — install manually for cabinet PDF features"
fi

# ---------------------------------------------------------------------------
# 5. install the migrate skill into ~/.claude/skills/ so any Claude Code
#    session on this machine has the orchestrator playbook available.
# ---------------------------------------------------------------------------
SKILL_SRC="${REPO_ROOT}/skills/migrate/SKILL.md"
SKILL_DEST_DIR="${HOME}/.claude/skills/migrate"
if [ -f "$SKILL_SRC" ]; then
  mkdir -p "$SKILL_DEST_DIR"
  if [ ! -e "$SKILL_DEST_DIR/SKILL.md" ] || ! cmp -s "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"; then
    cp "$SKILL_SRC" "$SKILL_DEST_DIR/SKILL.md"
    echo "[5/5] migrate skill: installed to $SKILL_DEST_DIR"
  else
    echo "[5/5] migrate skill: already up to date at $SKILL_DEST_DIR"
  fi
else
  echo "[5/5] migrate skill: source not found at $SKILL_SRC (skipping)"
fi

# ---------------------------------------------------------------------------
# Verify — non-fatal if `homing` isn't yet on PATH (shells started before
# ~/.local/bin was added won't see it; the user just needs to restart).
# ---------------------------------------------------------------------------
echo ""
echo "=== verify ==="
chezmoi --version | head -1
age --version
if python3 -c "import homing" 2>/dev/null; then
  python3 -c "import homing; print(f'homing v{getattr(homing, \"__version__\", \"unknown\")} from {homing.__file__}')"
else
  echo "homing: package installed but not yet importable in this shell"
  echo "        (open a new terminal so user-site is on sys.path)"
fi
[ -f "$SKILL_DEST_DIR/SKILL.md" ] && echo "migrate skill: $SKILL_DEST_DIR/SKILL.md"

echo ""
echo "=== done ==="
echo "Next: in a fresh Claude Code session, the migrate skill will be auto-discovered."
echo "Read BOOTSTRAP.md, then ask the user which mode they're in (A/B/C/D)."
echo "PATH addition: export PATH=\"\$HOME/.local/bin:\$PATH\" if 'homing' isn't found in a new shell."

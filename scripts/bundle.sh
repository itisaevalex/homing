#!/usr/bin/env bash
# bundle.sh — produce a physical-transfer migration bundle.
#
# Output structure (in $OUT):
#   dotfiles.tar.gz         chezmoi archive (bashrc, gitconfig, ~/.claude config, redshift, ...)
#   secrets.tar.gz.age      passphrase-encrypted ~/.config/secrets directory
#   system.tar.gz           homing's ~/system/ output (manifests, index, worklist) — optional
#   setup.sh                target-machine bootstrap script
#   MANIFEST.txt            file list with sha256 checksums
#   README.md               instructions
#
# The bundle is meant to live on an encrypted USB stick. Nothing here goes to a network
# transport. Secrets are double-protected (file mode 600 on source + age-encrypted in
# transit + on the destination disk inside the home dir).
#
# Usage:
#   ./bundle.sh                          # default: ~/migration-bundle-<ts>/
#   ./bundle.sh /media/usb/migration     # explicit output dir

set -euo pipefail

OUT=${1:-"$HOME/migration-bundle-$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$OUT"

CHEZMOI=${CHEZMOI:-$HOME/.local/bin/chezmoi}
AGE=${AGE:-$HOME/.local/bin/age}

if ! command -v "$CHEZMOI" >/dev/null && ! [ -x "$CHEZMOI" ]; then
  echo "error: chezmoi not found at $CHEZMOI" >&2
  exit 1
fi
if ! command -v "$AGE" >/dev/null && ! [ -x "$AGE" ]; then
  echo "error: age not found at $AGE" >&2
  exit 1
fi

echo "=== bundle output: $OUT ==="

# 1. dotfiles via chezmoi archive
echo "[1/5] chezmoi archive..."
"$CHEZMOI" archive --format tar.gz --output "$OUT/dotfiles.tar.gz"

# 2. secrets — tar then age-encrypt with passphrase
SECRETS_SRC="$HOME/.config/secrets"
SECRETS_PLAIN="$OUT/.secrets-plain.tar.gz"
# Trap ensures the plaintext tarball is destroyed even on Ctrl-C at the age
# passphrase prompt or any unexpected failure. Without this, an interrupt
# leaves cleartext secrets sitting on the migration media.
_cleanup_plain() {
  if [ -f "$SECRETS_PLAIN" ]; then
    shred -u "$SECRETS_PLAIN" 2>/dev/null || rm -f "$SECRETS_PLAIN"
  fi
}
trap _cleanup_plain EXIT INT TERM HUP
if [ -d "$SECRETS_SRC" ]; then
  # Exclude any stray bundle.key* files from the secrets tarball — these
  # only appear when an earlier `bundle.sh ... BUNDLE_KEY_MODE=key` run
  # leaked an identity file into ~/.config/secrets/. They're harmless to
  # destination but messy and would be re-bundled on every run.
  ( umask 077 && tar czf "$SECRETS_PLAIN" \
      --exclude='bundle.key' --exclude='bundle.key.*' \
      -C "$HOME/.config" secrets/ )
  AGE_KEYGEN=${AGE_KEYGEN:-$HOME/.local/bin/age-keygen}
  if [ "${BUNDLE_KEY_MODE:-passphrase}" = "key" ]; then
    # Non-interactive: generate an ephemeral keypair, encrypt with recipient
    # pubkey, ship the identity file as bundle.key alongside the bundle.
    # Suitable for trusted-channel transfers (e.g. point-to-point USB that
    # will be wiped). Carry bundle.key on a SEPARATE device or wipe with the
    # bundle once verified on destination.
    echo "[2/5] secrets: tar + age-encrypt (key mode — generating ephemeral keypair)..."
    KEYFILE="$OUT/bundle.key"
    ( umask 077 && "$AGE_KEYGEN" -o "$KEYFILE" 2>/dev/null )
    PUBKEY=$(grep '^# public key:' "$KEYFILE" | awk '{print $NF}')
    if [ -z "$PUBKEY" ]; then
      echo "error: failed to extract public key from $KEYFILE" >&2
      exit 1
    fi
    "$AGE" -r "$PUBKEY" --output "$OUT/secrets.tar.gz.age" "$SECRETS_PLAIN"
    chmod 600 "$KEYFILE"
  else
    echo "[2/5] secrets: tar + age-encrypt (you will be prompted for a passphrase)..."
    "$AGE" --passphrase --output "$OUT/secrets.tar.gz.age" "$SECRETS_PLAIN"
  fi
  _cleanup_plain
else
  echo "[2/5] no $SECRETS_SRC — skipping secrets"
fi
trap - EXIT INT TERM HUP

# 2b. credentials bundle — opt-in via INCLUDE_CREDS=1.
# By default these live out-of-band per BOOTSTRAP.md. Setting INCLUDE_CREDS=1
# adds a creds.tar.gz.age (encrypted with the same recipient as secrets) so
# trusted-channel transfers can ship everything in one bundle. SSH keys, AWS
# credentials, gh tokens, and gpg keyring (public material only) — no Docker
# buildx cache (regenerable) and no Tailscale state (system-level + re-auth).
if [ "${INCLUDE_CREDS:-0}" = "1" ]; then
  CREDS_PLAIN="$OUT/.creds-plain.tar.gz"
  _cleanup_creds_plain() {
    if [ -f "$CREDS_PLAIN" ]; then
      shred -u "$CREDS_PLAIN" 2>/dev/null || rm -f "$CREDS_PLAIN"
    fi
  }
  trap _cleanup_creds_plain EXIT INT TERM HUP

  CREDS_PATHS=()
  [ -d "$HOME/.ssh" ] && CREDS_PATHS+=(.ssh)
  [ -d "$HOME/.gnupg" ] && CREDS_PATHS+=(.gnupg)
  [ -d "$HOME/.aws" ] && CREDS_PATHS+=(.aws)
  [ -d "$HOME/.config/gh" ] && CREDS_PATHS+=(.config/gh)
  [ -f "$HOME/.docker/config.json" ] && CREDS_PATHS+=(.docker/config.json)
  # NSS DB (browser/system cert store, may hold TLS client private keys) and
  # Signal Desktop's local keyring (decrypts the message DB on a new machine).
  # Both are credentials in everything but name; if INCLUDE_CREDS is on the
  # user already accepted a creds bundle, no separate flag needed.
  [ -d "$HOME/.pki" ] && CREDS_PATHS+=(.pki)
  [ -f "$HOME/.config/Signal/config.json" ] && CREDS_PATHS+=(.config/Signal)
  [ -f "$HOME/signal-desktop-keyring.gpg" ] && CREDS_PATHS+=(signal-desktop-keyring.gpg)

  if [ ${#CREDS_PATHS[@]} -gt 0 ]; then
    echo "[2b] creds: tar + age-encrypt (${CREDS_PATHS[*]})..."
    ( umask 077 && tar czf "$CREDS_PLAIN" -C "$HOME" "${CREDS_PATHS[@]}" )
    if [ "${BUNDLE_KEY_MODE:-passphrase}" = "key" ]; then
      # Reuse the same bundle.key generated for secrets
      KEYFILE="$OUT/bundle.key"
      if [ ! -f "$KEYFILE" ]; then
        echo "error: BUNDLE_KEY_MODE=key but $KEYFILE missing" >&2
        exit 1
      fi
      PUBKEY=$(grep '^# public key:' "$KEYFILE" | awk '{print $NF}')
      "$AGE" -r "$PUBKEY" --output "$OUT/creds.tar.gz.age" "$CREDS_PLAIN"
    else
      echo "  (you will be prompted for the same passphrase used for secrets)"
      "$AGE" --passphrase --output "$OUT/creds.tar.gz.age" "$CREDS_PLAIN"
    fi
    _cleanup_creds_plain
  else
    echo "[2b] INCLUDE_CREDS=1 but none of .ssh/.gnupg/.aws/.config/gh/.docker found — skipping"
  fi
  trap - EXIT INT TERM HUP
fi

# 2c. wallets bundle — opt-in via INCLUDE_WALLETS=1.
# Crypto wallet keys are IRREPLACEABLE on chain — losing them = losing funds.
# By default these are out-of-band per BOOTSTRAP.md (the user is expected to
# carry seed phrases separately). Setting INCLUDE_WALLETS=1 ships them in a
# dedicated wallets.tar.gz.age — same age recipient as secrets/creds. Use
# this for trusted-channel transfers only; the failure mode of leaking this
# bundle is unbounded.
if [ "${INCLUDE_WALLETS:-0}" = "1" ]; then
  WALLETS_PLAIN="$OUT/.wallets-plain.tar.gz"
  _cleanup_wallets_plain() {
    if [ -f "$WALLETS_PLAIN" ]; then
      shred -u "$WALLETS_PLAIN" 2>/dev/null || rm -f "$WALLETS_PLAIN"
    fi
  }
  trap _cleanup_wallets_plain EXIT INT TERM HUP

  WALLET_PATHS=()
  # Monero — actual wallet keyfiles + node SSL key (which ties identity to
  # this node). Blockchain LMDB is regenerable but big; we include only if
  # explicitly opted in via INCLUDE_WALLETS_BLOCKCHAIN=1.
  [ -d "$HOME/Monero/wallets" ] && WALLET_PATHS+=(Monero/wallets)
  if [ -d "$HOME/.bitmonero" ]; then
    if [ "${INCLUDE_WALLETS_BLOCKCHAIN:-0}" = "1" ]; then
      WALLET_PATHS+=(.bitmonero)
    else
      # SSL cert/key only — small, identity-bearing
      [ -f "$HOME/.bitmonero/p2p_ssl.key" ] && WALLET_PATHS+=(.bitmonero/p2p_ssl.key)
      [ -f "$HOME/.bitmonero/p2p_ssl.crt" ] && WALLET_PATHS+=(.bitmonero/p2p_ssl.crt)
    fi
  fi
  [ -d "$HOME/.shared-ringdb" ] && [ "${INCLUDE_WALLETS_BLOCKCHAIN:-0}" = "1" ] \
    && WALLET_PATHS+=(.shared-ringdb)
  # Kaspa
  [ -d "$HOME/.kaspa" ] && WALLET_PATHS+=(.kaspa)
  [ -d "$HOME/.kdx" ] && WALLET_PATHS+=(.kdx)
  # Other common wallet locations
  [ -d "$HOME/.electrum" ] && WALLET_PATHS+=(.electrum)
  [ -d "$HOME/.bitcoin" ] && [ "${INCLUDE_WALLETS_BLOCKCHAIN:-0}" = "1" ] \
    && WALLET_PATHS+=(.bitcoin)
  [ -d "$HOME/.ethereum/keystore" ] && WALLET_PATHS+=(.ethereum/keystore)

  if [ ${#WALLET_PATHS[@]} -gt 0 ]; then
    echo "[2c] wallets: tar + age-encrypt (${WALLET_PATHS[*]})..."
    ( umask 077 && tar czf "$WALLETS_PLAIN" -C "$HOME" "${WALLET_PATHS[@]}" )
    if [ "${BUNDLE_KEY_MODE:-passphrase}" = "key" ]; then
      KEYFILE="$OUT/bundle.key"
      if [ ! -f "$KEYFILE" ]; then
        echo "error: BUNDLE_KEY_MODE=key but $KEYFILE missing" >&2
        exit 1
      fi
      PUBKEY=$(grep '^# public key:' "$KEYFILE" | awk '{print $NF}')
      "$AGE" -r "$PUBKEY" --output "$OUT/wallets.tar.gz.age" "$WALLETS_PLAIN"
    else
      echo "  (you will be prompted for the same passphrase used for secrets)"
      "$AGE" --passphrase --output "$OUT/wallets.tar.gz.age" "$WALLETS_PLAIN"
    fi
    _cleanup_wallets_plain
    echo "  WALLET BUNDLE CREATED — these keys are irreplaceable. Verify on destination BEFORE wiping the source."
  else
    echo "[2c] INCLUDE_WALLETS=1 but no known wallet locations found — skipping"
  fi
  trap - EXIT INT TERM HUP
fi

# 2d. extra personal dirs — opt-in via EXTRA_PERSONAL_DIRS env var.
# Top-level $HOME project dirs that aren't under Documents/ get missed by
# pack-personal.sh's defaults. This catches them as a sibling personal-extra/
# tarball in the bundle (un-encrypted; it's source code, not secrets).
# Format: colon-separated absolute or $HOME-relative paths.
#   EXTRA_PERSONAL_DIRS="$HOME/gnome-prism:$HOME/system-inventory:$HOME/opensource-staging"
if [ -n "${EXTRA_PERSONAL_DIRS:-}" ]; then
  EXTRA_PATHS=()
  IFS=':' read -ra _RAW_EXTRAS <<< "$EXTRA_PERSONAL_DIRS"
  for p in "${_RAW_EXTRAS[@]}"; do
    # Resolve to a path relative to $HOME for tar -C convenience.
    p="${p/#~/$HOME}"
    if [ -e "$p" ]; then
      rel="${p#$HOME/}"
      EXTRA_PATHS+=("$rel")
    else
      echo "  warning: EXTRA_PERSONAL_DIRS entry not found: $p"
    fi
  done
  if [ ${#EXTRA_PATHS[@]} -gt 0 ]; then
    echo "[2d] extras: tar (${EXTRA_PATHS[*]})..."
    # --warning=no-file-{changed,removed,shrunk} suppresses tar's exit-1
    # behavior on live-write races. Capturing live state (Claude conversation
    # logs, browser dbs, etc.) means files mutate mid-tar — without these
    # suppressors, set -euo pipefail aborts the whole bundle on a non-fatal
    # warning. Real fatal errors (exit >= 2) still propagate.
    tar czf "$OUT/personal-extra.tar.gz" \
        --warning=no-file-changed --warning=no-file-removed --warning=no-file-shrank \
        --exclude='node_modules' --exclude='.venv' --exclude='__pycache__' \
        --exclude='.cache' --exclude='target' --exclude='dist' \
        -C "$HOME" "${EXTRA_PATHS[@]}"
  fi
fi

# 2e. Claude Code session/conversation state — opt-in via INCLUDE_CLAUDE_STATE=1.
# ~/.claude/projects/ holds per-project conversation history (every chat
# Claude Code has had on this machine, organized by working directory).
# chezmoi explicitly ignores this in dotfiles repos because it churns
# constantly, so the dotfiles tarball misses it. For a real migration this
# is canonical: 100s of MB of context that would otherwise be lost.
# Encrypted because conversation history can include API keys, credentials,
# and project secrets discussed in chat.
if [ "${INCLUDE_CLAUDE_STATE:-0}" = "1" ]; then
  CLAUDE_PLAIN="$OUT/.claude-state-plain.tar.gz"
  _cleanup_claude_state() {
    if [ -f "$CLAUDE_PLAIN" ]; then
      shred -u "$CLAUDE_PLAIN" 2>/dev/null || rm -f "$CLAUDE_PLAIN"
    fi
  }
  trap _cleanup_claude_state EXIT INT TERM HUP

  CLAUDE_PATHS=()
  for p in \
      .claude/projects \
      .claude/session-data \
      .claude/sessions \
      .claude/todos \
      .claude/tasks \
      .claude/plans \
      .claude/history.jsonl \
      .claude/bash-commands.log \
      .claude/cost-tracker.log \
      .claude/file-history; do
    [ -e "$HOME/$p" ] && CLAUDE_PATHS+=("$p")
  done

  if [ ${#CLAUDE_PATHS[@]} -gt 0 ]; then
    echo "[2e] claude-state: tar + age-encrypt (${#CLAUDE_PATHS[@]} paths)..."
    # See [2d] note — Claude conversation logs and history.jsonl are written
    # live by any active Claude Code session. Suppress non-fatal tar warnings.
    ( umask 077 && tar czf "$CLAUDE_PLAIN" \
        --warning=no-file-changed --warning=no-file-removed --warning=no-file-shrank \
        -C "$HOME" "${CLAUDE_PATHS[@]}" )
    if [ "${BUNDLE_KEY_MODE:-passphrase}" = "key" ]; then
      KEYFILE="$OUT/bundle.key"
      if [ ! -f "$KEYFILE" ]; then
        echo "error: BUNDLE_KEY_MODE=key but $KEYFILE missing" >&2
        exit 1
      fi
      PUBKEY=$(grep '^# public key:' "$KEYFILE" | awk '{print $NF}')
      "$AGE" -r "$PUBKEY" --output "$OUT/claude-state.tar.gz.age" "$CLAUDE_PLAIN"
    else
      echo "  (you will be prompted for the same passphrase used for secrets)"
      "$AGE" --passphrase --output "$OUT/claude-state.tar.gz.age" "$CLAUDE_PLAIN"
    fi
    _cleanup_claude_state
  else
    echo "[2e] INCLUDE_CLAUDE_STATE=1 but no .claude/{projects,session-data,…} found — skipping"
  fi
  trap - EXIT INT TERM HUP
fi

# 3. homing system output (optional — only if it exists)
if [ -d "$HOME/system" ]; then
  echo "[3/5] homing ~/system/ snapshot..."
  tar czf "$OUT/system.tar.gz" \
      --exclude="*.sqlite-wal" --exclude="*.sqlite-shm" \
      -C "$HOME" system/
else
  echo "[3/5] no ~/system/ — skipping"
fi

# 4. setup.sh for target machine
echo "[4/5] writing setup.sh..."
cat > "$OUT/setup.sh" <<'SETUP_EOF'
#!/usr/bin/env bash
# setup.sh — apply this migration bundle on a fresh Ubuntu install.
#
# Usage: cd into the bundle directory, then:
#   bash setup.sh

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

echo "=== applying migration bundle from $HERE ==="

# 1. Tools — install chezmoi + age + git (no sudo unless absent)
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v chezmoi >/dev/null; then
  echo "[1/5] installing chezmoi to ~/.local/bin..."
  sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin"
fi

if ! command -v age >/dev/null; then
  echo "[1/5] installing age to ~/.local/bin..."
  curl -fsSL "https://github.com/FiloSottile/age/releases/download/v1.2.1/age-v1.2.1-linux-amd64.tar.gz" \
    -o /tmp/age.tar.gz
  tar xzf /tmp/age.tar.gz -C /tmp
  mv /tmp/age/age /tmp/age/age-keygen "$HOME/.local/bin/"
  chmod +x "$HOME/.local/bin/age" "$HOME/.local/bin/age-keygen"
  rm -rf /tmp/age /tmp/age.tar.gz
fi

# 2. Extract dotfiles directly into $HOME — `chezmoi archive` produces the
#    applied filesystem layout (e.g. .bashrc, not dot_bashrc), so no
#    `chezmoi apply` step is needed. We do still install chezmoi above so
#    the user can manage future drift with it.
echo "[2/5] extracting dotfiles into \$HOME..."
tar xzf "$HERE/dotfiles.tar.gz" -C "$HOME/"

# Optionally re-init chezmoi source from this archive so future `chezmoi diff`
# is meaningful on the target machine. Lives at the standard source path.
if command -v chezmoi >/dev/null; then
  chezmoi init --apply=false 2>/dev/null || true
fi

# 3. Decrypt secrets — uses bundle.key if present (key mode), else prompts for
#    the passphrase used at bundle time.
if [ -f "$HERE/secrets.tar.gz.age" ]; then
  if [ -f "$HERE/bundle.key" ]; then
    echo "[3/5] decrypting secrets using bundle.key..."
    AGE_DECRYPT_ARGS=(-d -i "$HERE/bundle.key")
  else
    echo "[3/5] decrypting secrets (you will be prompted for the bundle passphrase)..."
    AGE_DECRYPT_ARGS=(-d)
  fi
  # Per-user tmp so a multi-user box doesn't expose plaintext on /tmp.
  SECRETS_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/migrate-secrets.XXXXXX")
  chmod 700 "$SECRETS_TMPDIR"
  SECRETS_PLAIN="$SECRETS_TMPDIR/secrets-plain.tar.gz"
  _cleanup_secrets() {
    if [ -f "$SECRETS_PLAIN" ]; then
      shred -u "$SECRETS_PLAIN" 2>/dev/null || rm -f "$SECRETS_PLAIN"
    fi
    rm -rf "$SECRETS_TMPDIR" 2>/dev/null || true
  }
  trap _cleanup_secrets EXIT INT TERM HUP
  ( umask 077 && age "${AGE_DECRYPT_ARGS[@]}" -o "$SECRETS_PLAIN" "$HERE/secrets.tar.gz.age" )
  mkdir -p "$HOME/.config"
  tar xzf "$SECRETS_PLAIN" -C "$HOME/.config/"
  _cleanup_secrets
  trap - EXIT INT TERM HUP
  # Recursive permissions: dirs 700, files 600. The previous glob `*` skipped
  # dotfiles and didn't recurse into nested config trees.
  if [ -d "$HOME/.config/secrets" ]; then
    chmod 700 "$HOME/.config/secrets"
    find "$HOME/.config/secrets" -type d -exec chmod 700 {} +
    find "$HOME/.config/secrets" -type f -exec chmod 600 {} +
  fi
else
  echo "[3/5] no secrets.tar.gz.age in bundle — skipping secrets"
fi

# 3b. Decrypt + extract creds bundle if shipped (opt-in via INCLUDE_CREDS=1
#     at bundle time). Restores .ssh/.gnupg/.aws/.config/gh/.docker/config.json.
if [ -f "$HERE/creds.tar.gz.age" ]; then
  echo "[3b] decrypting + extracting creds bundle..."
  CREDS_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/migrate-creds.XXXXXX")
  chmod 700 "$CREDS_TMPDIR"
  CREDS_PLAIN="$CREDS_TMPDIR/creds-plain.tar.gz"
  _cleanup_creds() {
    if [ -f "$CREDS_PLAIN" ]; then
      shred -u "$CREDS_PLAIN" 2>/dev/null || rm -f "$CREDS_PLAIN"
    fi
    rm -rf "$CREDS_TMPDIR" 2>/dev/null || true
  }
  trap _cleanup_creds EXIT INT TERM HUP
  ( umask 077 && age "${AGE_DECRYPT_ARGS[@]}" -o "$CREDS_PLAIN" "$HERE/creds.tar.gz.age" )
  tar xzf "$CREDS_PLAIN" -C "$HOME/"
  _cleanup_creds
  trap - EXIT INT TERM HUP
  # Lock down credential dirs — tar may have preserved modes but be defensive.
  for d in "$HOME/.ssh" "$HOME/.gnupg" "$HOME/.aws" "$HOME/.config/gh"; do
    if [ -d "$d" ]; then
      chmod 700 "$d"
      find "$d" -type d -exec chmod 700 {} +
      find "$d" -type f -exec chmod 600 {} +
    fi
  done
  # Public-key files conventionally stay world-readable; restore that for .pub
  find "$HOME/.ssh" -name '*.pub' -type f -exec chmod 644 {} + 2>/dev/null || true
fi

# 3c. Decrypt + extract wallets bundle if shipped (opt-in INCLUDE_WALLETS=1).
#     Crypto wallet keys are irreplaceable — verify decryption + presence
#     before wiping the source.
if [ -f "$HERE/wallets.tar.gz.age" ]; then
  echo "[3c] decrypting + extracting wallets bundle..."
  WALLETS_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/migrate-wallets.XXXXXX")
  chmod 700 "$WALLETS_TMPDIR"
  WALLETS_PLAIN="$WALLETS_TMPDIR/wallets-plain.tar.gz"
  _cleanup_wallets() {
    if [ -f "$WALLETS_PLAIN" ]; then
      shred -u "$WALLETS_PLAIN" 2>/dev/null || rm -f "$WALLETS_PLAIN"
    fi
    rm -rf "$WALLETS_TMPDIR" 2>/dev/null || true
  }
  trap _cleanup_wallets EXIT INT TERM HUP
  ( umask 077 && age "${AGE_DECRYPT_ARGS[@]}" -o "$WALLETS_PLAIN" "$HERE/wallets.tar.gz.age" )
  tar xzf "$WALLETS_PLAIN" -C "$HOME/"
  _cleanup_wallets
  trap - EXIT INT TERM HUP
  # Lock down wallet dirs — tar should preserve modes but be defensive.
  for d in "$HOME/Monero" "$HOME/.kaspa" "$HOME/.kdx" "$HOME/.electrum" \
           "$HOME/.bitcoin" "$HOME/.ethereum" "$HOME/.bitmonero" \
           "$HOME/.shared-ringdb"; do
    if [ -d "$d" ]; then
      chmod 700 "$d"
      find "$d" -type d -exec chmod 700 {} + 2>/dev/null
      find "$d" -type f -exec chmod 600 {} + 2>/dev/null
    fi
  done
  echo "  WALLET BUNDLE EXTRACTED — verify wallets open before wiping the source drive."
fi

# 3d. Extract personal-extra (top-level project dirs not under Documents/).
if [ -f "$HERE/personal-extra.tar.gz" ]; then
  echo "[3d] extracting personal-extra into \$HOME..."
  tar xzf "$HERE/personal-extra.tar.gz" -C "$HOME/"
fi

# 3e. Decrypt + extract Claude Code session/conversation state.
#     Restores ~/.claude/projects (per-project conversation history),
#     session-data, sessions, todos, tasks, plans, history.jsonl.
if [ -f "$HERE/claude-state.tar.gz.age" ]; then
  echo "[3e] decrypting + extracting Claude Code state..."
  CLAUDE_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/migrate-claude.XXXXXX")
  chmod 700 "$CLAUDE_TMPDIR"
  CLAUDE_PLAIN="$CLAUDE_TMPDIR/claude-plain.tar.gz"
  _cleanup_claude() {
    if [ -f "$CLAUDE_PLAIN" ]; then
      shred -u "$CLAUDE_PLAIN" 2>/dev/null || rm -f "$CLAUDE_PLAIN"
    fi
    rm -rf "$CLAUDE_TMPDIR" 2>/dev/null || true
  }
  trap _cleanup_claude EXIT INT TERM HUP
  ( umask 077 && age "${AGE_DECRYPT_ARGS[@]}" -o "$CLAUDE_PLAIN" "$HERE/claude-state.tar.gz.age" )
  tar xzf "$CLAUDE_PLAIN" -C "$HOME/"
  _cleanup_claude
  trap - EXIT INT TERM HUP
fi

# 4. Restore homing's system/ output if present
if [ -f "$HERE/system.tar.gz" ]; then
  echo "[4/5] restoring ~/system/ ..."
  tar xzf "$HERE/system.tar.gz" -C "$HOME/"
fi

# 5. Enable systemd user units that came across
echo "[5/5] enabling systemd user units (redshift)..."
if [ -d "$HOME/.config/systemd/user" ]; then
  systemctl --user daemon-reload || true
  for unit in redshift-day.timer redshift-night.timer; do
    if [ -f "$HOME/.config/systemd/user/$unit" ]; then
      systemctl --user enable --now "$unit" 2>/dev/null || true
    fi
  done
fi

cat <<EOF

=== migration applied ===

Next steps you do manually:
  1. Open a fresh shell so ~/.bashrc + secrets load.
  2. Verify env vars: env | grep -E 'STRIPE|SUPABASE|RESEND|FIRECRAWL'
  3. If creds were NOT included in this bundle, restore SSH/GPG/AWS via your
     separate channel. If they WERE included (creds.tar.gz.age present), they
     are now extracted to ~/.ssh, ~/.gnupg, ~/.aws — verify with:
        ssh -T git@github.com
        aws sts get-caller-identity
  4. tailscale up --auth-key=<from your password manager>
  5. gh auth status   (if creds bundled, token is already restored)

What this bundle did NOT migrate, by design:
  - Tailscale state (system-level; re-auth is the canonical path)
  - Docker buildx cache (regenerable)
  - Browser saved passwords (use Firefox Sync / Chrome sync)
  - Project source code (clone fresh from GitHub)
  - Browser bookmarks/passwords (use Firefox Sync / Chrome sync)
  - Project source code (clone from GitHub fresh)

EOF
SETUP_EOF
chmod +x "$OUT/setup.sh"

# 5. Manifest + checksums + README
echo "[5/5] writing MANIFEST + README..."
(cd "$OUT" && find . -maxdepth 1 -type f -not -name MANIFEST.txt -not -name README.md \
   -exec sha256sum {} \;) > "$OUT/MANIFEST.txt"

cat > "$OUT/README.md" <<README_EOF
# Migration bundle

- Generated: $(date -Iseconds)
- Source machine: $(hostname)
- Source user: $(whoami)
- Bundle path: $OUT

## Files

| File | Purpose |
|---|---|
| dotfiles.tar.gz | chezmoi archive — applied via 'chezmoi apply' on target |
| secrets.tar.gz.age | age-encrypted ~/.config/secrets — passphrase needed |
| system.tar.gz | homing's ~/system/ output (manifests, index) if present |
| setup.sh | target-machine bootstrap |
| MANIFEST.txt | sha256 checksums for integrity check |

## How to use

On a fresh Ubuntu (or any Linux with bash):

\`\`\`bash
# Verify bundle integrity
sha256sum -c MANIFEST.txt

# Apply
bash setup.sh
\`\`\`

You'll be prompted once for the age passphrase that encrypted the secrets.

## What's NOT in this bundle (by design)

- SSH/GPG/age private keys — transfer through a separate encrypted channel (Yubikey, second USB, password manager)
- AWS credentials — re-enter via 'aws configure' on target
- Browser profiles — use Firefox Sync / Chrome sync if you want them
- Project source — clone fresh from GitHub
- Honcho data — already lives on the home server, just point at it from the new machine

## Threat model

- Bundle on USB: protected by age passphrase. Lose the USB, attacker still needs the passphrase.
- Bundle in transit: same.
- Bundle on target: secrets are decrypted into ~/.config/secrets (mode 600). On a single-user laptop with full-disk encryption, this is fine. On a shared machine, don't run setup.sh.
README_EOF

echo ""
echo "=== bundle complete ==="
ls -la "$OUT"
echo ""
echo "Total size: $(du -sh "$OUT" | awk '{print $1}')"
echo ""
echo "Transfer this directory to your USB. To apply on a new machine:"
echo "  cd <bundle-dir> && bash setup.sh"

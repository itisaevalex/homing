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

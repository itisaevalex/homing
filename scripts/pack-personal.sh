#!/usr/bin/env bash
# pack-personal.sh — rsync personal-data folders to the migration drive.
#
# Bulk byte-mover — runs after cabinet's choose-and-organize pass.
#
# Usage: ./pack-personal.sh <output-dir> [target...]
# Default targets: ~/Documents ~/Pictures ~/Music ~/Videos ~/Desktop
#
# Exit codes:
#   0  — every target rsynced cleanly (or was missing/skipped, never an error)
#   1  — at least one rsync exited non-zero. Output dir kept for triage.
#   2  — usage error (missing arg, etc).

set -euo pipefail

OUT=${1:?"usage: pack-personal.sh <output-dir> [target...]"}
shift || true
TARGETS=("$@")
if [ "${#TARGETS[@]}" -eq 0 ]; then
  TARGETS=(
    "$HOME/Documents"
    "$HOME/Pictures"
    "$HOME/Music"
    "$HOME/Videos"
    "$HOME/Desktop"
  )
fi

mkdir -p "$OUT"
chmod 700 "$OUT"

# Detect destination filesystem so we can downgrade flags on FAT/exFAT/ntfs
# (which don't preserve owner, xattrs, or POSIX ACLs). Without this, rsync
# emits a wall of -X / -A errors on non-POSIX targets.
RSYNC_FLAGS="-aH"
DEST_FSTYPE=$(df --output=fstype "$OUT" 2>/dev/null | tail -1 | tr -d '[:space:]' || true)
case "$DEST_FSTYPE" in
  vfat|fat|fat32|exfat|msdos|ntfs|ntfs3|fuseblk)
    echo "  note: destination is $DEST_FSTYPE — disabling -A (ACLs) and -X (xattrs)"
    ;;
  *)
    # Concatenate flags into the same arg (rsync wants -aHAX, not -aH AX which
    # word-splits into a phantom filename).
    RSYNC_FLAGS="${RSYNC_FLAGS}AX"
    ;;
esac

EXCLUDES=(
  --exclude='*.tmp'
  --exclude='.cache'
  --exclude='Thumbs.db'
  --exclude='.DS_Store'
  --exclude='__pycache__'
  --exclude='node_modules'
  --exclude='.venv'
  --exclude='venv'
  --exclude='*.pyc'
)

echo "=== pack-personal: $OUT ==="
echo "Targets:"
for t in "${TARGETS[@]}"; do
  if [ -d "$t" ]; then
    sz=$(du -sh "$t" 2>/dev/null | awk '{print $1}')
    printf "  %-40s %s\n" "$t" "$sz"
  else
    printf "  %-40s (missing - skipping)\n" "$t"
  fi
done
echo ""

WROTE_ANY=0
ANY_FAILED=0
for t in "${TARGETS[@]}"; do
  [ -d "$t" ] || continue
  rel=$(basename "$t")
  echo "  rsyncing $t -> $OUT/$rel/"
  # We want the script to continue across multiple targets even if one
  # rsync errors (transient, partial, etc.) but still surface failure as
  # exit code 1 at the end. With `set -e`, failing the pipeline would kill
  # the script before we could record it. Use `set +e` around the call,
  # capture PIPESTATUS[0] BEFORE any subsequent command resets it, restore
  # `set -e`. `|| true` is wrong here — it makes PIPESTATUS reflect the
  # successful `true`, masking rsync failures.
  set +e
  rsync $RSYNC_FLAGS "${EXCLUDES[@]}" "$t/" "$OUT/$rel/" 2>&1 | tail -3
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "  WARNING: rsync $t exited with code $rc"
    ANY_FAILED=1
  fi
  WROTE_ANY=1
done

if [ "$WROTE_ANY" -eq 1 ]; then
  echo ""
  echo "=== personal data snapshot complete ==="
  du -sh "$OUT"/* 2>/dev/null | sort -h || true
fi

if [ "$ANY_FAILED" -ne 0 ]; then
  echo ""
  echo "=== one or more rsync passes reported errors — review the output above ==="
  exit 1
fi

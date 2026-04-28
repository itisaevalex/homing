#!/usr/bin/env bash
# pack-personal.sh — rsync personal-data folders to the migration drive.
#
# Bulk byte-mover — runs after cabinet's choose-and-organize pass.
#
# Usage: ./pack-personal.sh <output-dir> [target...]
# Default targets: ~/Documents ~/Pictures ~/Music ~/Videos ~/Desktop

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
for t in "${TARGETS[@]}"; do
  [ -d "$t" ] || continue
  rel=$(basename "$t")
  echo "  rsyncing $t -> $OUT/$rel/"
  rsync -aHAX "${EXCLUDES[@]}" "$t/" "$OUT/$rel/" 2>&1 | tail -3
  WROTE_ANY=1
done

if [ "$WROTE_ANY" -eq 1 ]; then
  echo ""
  echo "=== personal data snapshot complete ==="
  du -sh "$OUT"/* | sort -h
fi

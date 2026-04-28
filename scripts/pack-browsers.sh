#!/usr/bin/env bash
# pack-browsers.sh — snapshot installed browser profiles for migration.
#
# !!!  SECURITY NOTICE  !!!
# This script captures full browser profile trees, which include cookies,
# saved passwords (encrypted with the user's master key + OS keyring),
# session tokens, and complete browsing history. Treat the OUTPUT
# DIRECTORY as you would your password vault:
#   - Chmod-700'd from the start.
#   - Move it to encrypted storage (age, LUKS, age-encrypted USB) before
#     leaving the machine.
#   - Do NOT email, cloud-sync, or git-push it.
#
# Captures Firefox, Cursor, Chromium-family profiles plus portable exports
# (bookmarks.html, tabs.json, history.json) so even if profile-dir restoration
# fails on the target machine, the user's bookmarks, open tabs, and history
# survive in a portable form.
#
# Usage: ./pack-browsers.sh <output-dir>
#
# IMPORTANT: close all browsers before running. SQLite databases get locked
# while the browser is open and the snapshot will be inconsistent. The script
# will ABORT (not just warn) if it detects a running browser.

set -euo pipefail

OUT=${1:?"usage: pack-browsers.sh <output-dir>"}
mkdir -p "$OUT"
# Lock the output dir to the current user from the very start. Profile data
# contains cookies and session tokens; world-readable would be a credential
# leak.
chmod 700 "$OUT"

EXCLUDES=(
  --exclude='Cache*'
  --exclude='cache2'
  --exclude='startupCache'
  --exclude='Code Cache'
  --exclude='GPUCache'
  --exclude='OfflineCache'
  --exclude='Service Worker/CacheStorage'
  --exclude='ScriptCache'
  --exclude='shader-cache'
  --exclude='thumbnails'
  --exclude='Cookies-journal'
  --exclude='Crash Reports'
  --exclude='Reporting and NEL'
  --exclude='*.lock'
  --exclude='lock'
  --exclude='Singleton*'
  # SQLite WAL/SHM are runtime-only artefacts. Including them when the
  # source DB is closed is harmless but bigger; including them when it's
  # open captures inconsistent state. Either way, drop them.
  --exclude='*.sqlite-wal'
  --exclude='*.sqlite-shm'
  --exclude='*-journal'
)

# Detect destination filesystem to downgrade rsync flags on non-POSIX FS
# (FAT/exFAT/NTFS don't support xattrs or ACLs; rsync would error otherwise).
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

abort_if_running() {
  local proc="$1"
  if pgrep -f "$proc" >/dev/null 2>&1; then
    echo "  ERROR: '$proc' is running. Close it before snapshotting." >&2
    echo "         Open browsers hold SQLite locks; snapshots taken now will be" >&2
    echo "         inconsistent and may be unrecoverable on the target machine." >&2
    exit 1
  fi
}

# Run rsync, capture its exit via PIPESTATUS, surface non-zero codes.
# Returns rsync's exit code so callers can decide whether to bail.
#
# Implementation note: with `set -e -o pipefail` (set at top of file), a
# failing rsync would kill the script before we could capture its exit.
# `set +e`/`set -e` around the pipeline lets the call continue, and we
# read PIPESTATUS[0] BEFORE any other command runs (since the next
# foreground pipeline replaces PIPESTATUS).
run_rsync() {
  local rc
  set +e
  rsync $RSYNC_FLAGS "${EXCLUDES[@]}" "$@" 2>&1 | tail -3
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

ANY_FAILED=0

# Firefox
if [ -d "$HOME/.mozilla/firefox" ]; then
  echo "[firefox] snapshotting ~/.mozilla/firefox/"
  abort_if_running firefox-bin
  abort_if_running 'firefox$'
  mkdir -p "$OUT/firefox"
  if ! run_rsync "$HOME/.mozilla/firefox/" "$OUT/firefox/profile-tree/"; then
    echo "  WARNING: firefox profile-tree rsync reported errors"
    ANY_FAILED=1
  fi
  echo "  profile tree: $(du -sh "$OUT/firefox/profile-tree/" | awk '{print $1}')"

  for profile_dir in "$OUT/firefox/profile-tree/"*default* ; do
    [ -d "$profile_dir" ] || continue
    name=$(basename "$profile_dir")

    # Bookmarks → portable bookmarks.html (importable by any browser).
    if [ -f "$profile_dir/places.sqlite" ]; then
      python3 - "$profile_dir/places.sqlite" "$OUT/firefox/$name.bookmarks.html" <<'PYEOF' 2>&1 | head -2
import sys, sqlite3, html
db, out = sys.argv[1:]
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT b.title, p.url
        FROM moz_bookmarks b
        JOIN moz_places p ON p.id = b.fk
        WHERE b.type = 1
        ORDER BY b.id
    """).fetchall()
    with open(out, 'w') as f:
        f.write("<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<H1>Bookmarks</H1>\n<DL><p>\n")
        for title, url in rows:
            if url:
                f.write(f'  <DT><A HREF="{html.escape(url)}">{html.escape(title or url)}</A>\n')
        f.write("</DL>\n")
    print(f"  bookmarks export: {len(rows)} items -> {out}")
except Exception as e:
    print(f"  bookmarks export: SKIP ({e})")
PYEOF

      # History → portable history.json. Captures URL, title, visit count,
      # last visit time. The agent on the target machine reads this and
      # decides what (if anything) to import — usually we don't restore
      # history blindly, but having it as a queryable artifact is useful
      # ("when did I last hit that vendor's docs?").
      python3 - "$profile_dir/places.sqlite" "$OUT/firefox/$name.history.json" <<'PYEOF' 2>&1 | head -2
import sys, sqlite3, json
db, out = sys.argv[1:]
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT url, title, visit_count, last_visit_date
        FROM moz_places
        WHERE hidden = 0 AND visit_count > 0
        ORDER BY last_visit_date DESC
    """).fetchall()
    history = []
    for url, title, vc, lvd in rows:
        history.append({
            "url": url,
            "title": title or "",
            "visit_count": int(vc or 0),
            # Firefox stores last_visit_date as microseconds since epoch.
            "last_visit_unix": (int(lvd) // 1_000_000) if lvd else None,
        })
    with open(out, 'w') as f:
        json.dump({"profile": __import__("os").path.basename(out), "count": len(history), "history": history}, f, indent=2, ensure_ascii=False)
    print(f"  history export: {len(history)} entries -> {out}")
except Exception as e:
    print(f"  history export: SKIP ({e})")
PYEOF
    fi

    # Open tabs → portable tabs.json.
    if [ -f "$profile_dir/sessionstore.jsonlz4" ]; then
      python3 - "$profile_dir/sessionstore.jsonlz4" "$OUT/firefox/$name.tabs.json" <<'PYEOF' 2>&1 | head -2
import sys, json
src, out = sys.argv[1:]
try:
    import lz4.block
    with open(src, 'rb') as f:
        f.read(8)
        raw = lz4.block.decompress(f.read())
    data = json.loads(raw)
    tabs = []
    for w in data.get('windows', []):
        for t in w.get('tabs', []):
            entries = t.get('entries', [])
            if entries:
                e = entries[-1]
                tabs.append({'title': e.get('title', ''), 'url': e.get('url', '')})
    with open(out, 'w') as f:
        json.dump({'window_count': len(data.get('windows', [])), 'tab_count': len(tabs), 'tabs': tabs}, f, indent=2, ensure_ascii=False)
    print(f"  tabs export: {len(tabs)} tabs -> {out}")
except ImportError:
    print(f"  tabs export: SKIP (pip install lz4 to enable)")
except Exception as e:
    print(f"  tabs export: SKIP ({e})")
PYEOF
    fi
  done
fi

# Cursor
if [ -d "$HOME/.config/Cursor" ]; then
  echo "[cursor] snapshotting ~/.config/Cursor/User/ + extensions list"
  abort_if_running 'cursor$'
  mkdir -p "$OUT/cursor"
  if ! run_rsync "$HOME/.config/Cursor/User/" "$OUT/cursor/User/"; then
    echo "  WARNING: cursor profile rsync reported errors"
    ANY_FAILED=1
  fi
  if [ -d "$HOME/.cursor/extensions" ]; then
    ls "$HOME/.cursor/extensions/" | grep -v '\.json$' > "$OUT/cursor/extensions-list.txt" 2>/dev/null || true
    [ -s "$OUT/cursor/extensions-list.txt" ] && echo "  extensions: $(wc -l < "$OUT/cursor/extensions-list.txt") -> extensions-list.txt"
  fi
fi

# Chromium-family
chromium_export_portable() {
  # Args: <browser-name> <profile-dir-relative-to-config-root>
  # Reads Bookmarks (JSON file) and History (SQLite) and emits portable
  # bookmarks.json and history.json next to the snapshot.
  local browser="$1"
  local profile_root="$2"
  local profile_path="$HOME/.config/$browser/$profile_root"
  [ -d "$profile_path" ] || return 0

  if [ -f "$profile_path/Bookmarks" ]; then
    cp "$profile_path/Bookmarks" "$OUT/$browser/${profile_root//\//_}.bookmarks.json" 2>/dev/null || true
  fi

  if [ -f "$profile_path/History" ]; then
    python3 - "$profile_path/History" "$OUT/$browser/${profile_root//\//_}.history.json" <<'PYEOF' 2>&1 | head -2
import sys, sqlite3, json, os, shutil, tempfile
db, out = sys.argv[1:]
# Chromium locks History while running and even after close keeps a hot
# WAL. We copy it to a tmp file before opening read-only to avoid any
# residual lock issues on machines where the browser was running today.
try:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        shutil.copyfile(db, tmp.name)
        tmp_path = tmp.name
    conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
    rows = conn.execute("""
        SELECT url, title, visit_count, last_visit_time
        FROM urls
        WHERE hidden = 0 AND visit_count > 0
        ORDER BY last_visit_time DESC
    """).fetchall()
    history = []
    # Chromium's last_visit_time is microseconds since 1601-01-01 UTC
    # ("Windows epoch"). 11644473600 = seconds between 1601 and 1970.
    EPOCH_OFFSET = 11644473600
    for url, title, vc, lvt in rows:
        unix = None
        if lvt:
            unix = (int(lvt) // 1_000_000) - EPOCH_OFFSET
            if unix < 0:
                unix = None
        history.append({
            "url": url,
            "title": title or "",
            "visit_count": int(vc or 0),
            "last_visit_unix": unix,
        })
    with open(out, 'w') as f:
        json.dump({"profile": os.path.basename(out), "count": len(history), "history": history}, f, indent=2, ensure_ascii=False)
    os.unlink(tmp_path)
    print(f"  history export: {len(history)} entries -> {out}")
except Exception as e:
    print(f"  history export: SKIP ({e})")
PYEOF
  fi
}

for browser in chromium google-chrome opera vivaldi brave-browser; do
  src="$HOME/.config/$browser"
  if [ -d "$src" ]; then
    echo "[$browser] snapshotting $src"
    abort_if_running "$browser"
    mkdir -p "$OUT/$browser"
    if ! run_rsync "$src/" "$OUT/$browser/"; then
      echo "  WARNING: $browser profile rsync reported errors"
      ANY_FAILED=1
    fi
    # Portable exports for the Default profile (most users only have one).
    chromium_export_portable "$browser" "Default"
  fi
done

# README
cat > "$OUT/README.md" <<EOF
# Browser snapshot

Generated: $(date -Iseconds)
Source: $(hostname)

**This directory contains cookies, session tokens, saved passwords (encrypted),
and complete browsing history. It is chmod 700 from creation. Move it to
encrypted storage (age tarball, LUKS volume, age-encrypted USB) before leaving
the machine.**

## Restore on new machine — close every browser FIRST

\`\`\`bash
# Firefox
rsync -aHA <this-dir>/firefox/profile-tree/ ~/.mozilla/firefox/

# Cursor
rsync -aHA <this-dir>/cursor/User/ ~/.config/Cursor/User/
while read ext; do cursor --install-extension "\$ext"; done < <this-dir>/cursor/extensions-list.txt

# Chromium-family (substitute name)
rsync -aHA <this-dir>/<name>/ ~/.config/<name>/
\`\`\`

## Portable fallback exports

Each profile produces:

- \`<profile>.bookmarks.html\` (Firefox) / \`.bookmarks.json\` (Chromium) — importable by any browser
- \`<profile>.tabs.json\` (Firefox) — last-saved set of open tabs
- \`<profile>.history.json\` — full browsing history with URL, title, visit count, last_visit_unix

If the profile-tree restore fails on the target machine, these portable JSON
exports are enough to reconstruct bookmarks, tabs, and history queries.

Caches and lock files are excluded — regenerable, would slow rsync. SQLite WAL
and SHM files are also excluded (runtime artefacts).
EOF

echo ""
echo "=== browser snapshot complete ==="
du -sh "$OUT"/* 2>/dev/null | sort -h || true
echo "Total: $(du -sh "$OUT" | awk '{print $1}')"

if [ "$ANY_FAILED" -ne 0 ]; then
  echo ""
  echo "=== one or more rsync passes reported errors — review the output above ==="
  exit 1
fi

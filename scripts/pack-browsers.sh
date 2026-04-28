#!/usr/bin/env bash
# pack-browsers.sh — snapshot installed browser profiles for migration.
#
# Captures Firefox, Cursor, Chromium-family profiles plus portable exports
# (bookmarks.html, tabs.json) so even if profile-dir restoration fails on the
# target machine, the user's bookmarks and open tabs survive in a portable form.
#
# Usage: ./pack-browsers.sh <output-dir>
#
# IMPORTANT: close all browsers before running. SQLite databases get locked
# while the browser is open and the snapshot will be inconsistent.

set -euo pipefail

OUT=${1:?"usage: pack-browsers.sh <output-dir>"}
mkdir -p "$OUT"

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
)

warn_if_running() {
  local proc="$1"
  if pgrep -f "$proc" >/dev/null 2>&1; then
    echo "  WARNING: '$proc' appears to be running. Close it first to avoid locked SQLite databases."
  fi
}

# Firefox
if [ -d "$HOME/.mozilla/firefox" ]; then
  echo "[firefox] snapshotting ~/.mozilla/firefox/"
  warn_if_running firefox-bin
  mkdir -p "$OUT/firefox"
  rsync -aHA "${EXCLUDES[@]}" "$HOME/.mozilla/firefox/" "$OUT/firefox/profile-tree/" 2>&1 | tail -3
  echo "  profile tree: $(du -sh "$OUT/firefox/profile-tree/" | awk '{print $1}')"

  for profile_dir in "$OUT/firefox/profile-tree/"*default* ; do
    [ -d "$profile_dir" ] || continue
    name=$(basename "$profile_dir")

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
    fi

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
  warn_if_running cursor
  mkdir -p "$OUT/cursor"
  rsync -aHA "${EXCLUDES[@]}" "$HOME/.config/Cursor/User/" "$OUT/cursor/User/" 2>&1 | tail -2
  if [ -d "$HOME/.cursor/extensions" ]; then
    ls "$HOME/.cursor/extensions/" | grep -v '\.json$' > "$OUT/cursor/extensions-list.txt" 2>/dev/null || true
    [ -s "$OUT/cursor/extensions-list.txt" ] && echo "  extensions: $(wc -l < "$OUT/cursor/extensions-list.txt") -> extensions-list.txt"
  fi
fi

# Chromium-family
for browser in chromium google-chrome opera vivaldi brave-browser; do
  src="$HOME/.config/$browser"
  if [ -d "$src" ]; then
    echo "[$browser] snapshotting $src"
    warn_if_running "$browser"
    mkdir -p "$OUT/$browser"
    rsync -aHA "${EXCLUDES[@]}" "$src/" "$OUT/$browser/" 2>&1 | tail -2
    bm="$src/Default/Bookmarks"
    [ -f "$bm" ] && cp "$bm" "$OUT/$browser/bookmarks.json"
  fi
done

# README
cat > "$OUT/README.md" <<EOF
# Browser snapshot

Generated: $(date -Iseconds)
Source: $(hostname)

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

Each Firefox default profile produced \`<profile>.bookmarks.html\` (importable in any browser)
and \`<profile>.tabs.json\` (readable list of open tabs). If profile-dir restore fails on the
new machine, these cover the bookmarks + tabs subset.

Caches and lock files are excluded — regenerable, would slow rsync.
Cookies and saved logins ARE included via profile-tree.
EOF

echo ""
echo "=== browser snapshot complete ==="
du -sh "$OUT"/* 2>/dev/null | sort -h
echo "Total: $(du -sh "$OUT" | awk '{print $1}')"

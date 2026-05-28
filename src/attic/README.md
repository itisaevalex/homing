# attic

A third sibling CLI alongside [`homing`](../homing/) and [`cabinet`](../cabinet/). Evicts **cold data** from local disk to an encrypted object-storage remote — "upload once, download rarely" — so you can free a laptop without losing anything.

> **Eviction, not backup.** A backup keeps the local copy as a safety net. attic *replaces* the local copy with a proof of recoverability. That's a separate problem from what `restic` solves (see [`INTENT.md`](../../INTENT.md)).

## The load-bearing safety rule

`evict` (the only command that deletes local data) deletes a local copy **only after** a real download → decrypt → re-hash round trip proves the bytes are independently recoverable under the currently-loaded crypt config. It never trusts `rclone cryptcheck` or rclone exit codes — both can pass while the data is unrecoverable (rotated keys, async-tier objects, backends without end-to-end hashing).

`push` and `evict` are decoupled: `push` uploads + verifies but **never deletes**; `evict` is a separate step the human triggers later, after re-verifying.

## Quick start

```bash
# 0. Prerequisites
rclone version                        # rclone must be on PATH
rclone config create my-s3 s3 ...     # configure a backing remote
                                      # (or B2, R2, Google Cloud Storage, Azure Blob, etc.)

# 1. Init — wraps your backing remote in a crypt remote, generates the password,
#    refuses subsequent push until you've stored the password somewhere off-box.
attic init --remote-backend my-s3

# 2. Plan — score paths for archival, write an ArchivePlan JSON.
attic plan ~/cold-stuff --min-size 100000000 --min-age-days 90

# 3. Push — upload + per-unit round-trip verify. Never deletes local.
attic push --plan ~/attic/plans/plan-*.json --confirmed

# 4. (Later, when you're sure) Evict — re-verify against the live remote,
#    write a tombstone, then delete local.
attic evict --ledger ~/attic/ledgers/push-*.jsonl --confirmed

# 5. Restore — download + decrypt + verify + place. Refuses to overwrite an
#    existing target. Reproduces empty dirs + symlinks from the manifest.
attic restore <unit_id> --confirmed
```

## Pipeline

```
homing index ──▶ attic candidates ──▶ attic plan ──▶ attic push    ──▶ attic evict   ──▶ attic restore
(legibility)     (Phase 2 — read     (ArchivePlan)   (upload +         (round-trip       (download +
                  ~/system/index.     JSON)           VERIFY,           re-verify, then   verify, atomic
                  json — Phase 1                      never deletes)    delete local)     place)
                  uses explicit
                  paths)
```

## Why a directory needs a manifest, not just bytes

Object stores don't preserve empty directories, and `rclone --links` rewrites symlinks as `.rclonelink` files. If attic relied on a single tree hash and the byte-stream alone, a perfect "successful upload" would still drop empty dirs and shape-shift symlinks on restore. So a dir unit carries an explicit manifest in the ledger entry — every file with its sha256+size+mode, every symlink with its target, every empty dir — and `restore` rebuilds the tree from that manifest, then asserts every entry matches before atomic-renaming into place.

## Files in this package

| File | Role |
|---|---|
| `transport.py` | rclone subprocess wrapper. Never trusts exit codes for verification; size + count is a separate explicit check. |
| `ledger.py` | Append-only JSONL + fsync. `LedgerEntry` schema, `build_manifest()`, `content_hash()`. Imports cabinet's `_hash_dir_tree`/`_fingerprint` so attic's hashes match cabinet's. |
| `operations.py` | `do_push` / `do_evict` / `do_restore` + the trusted round-trip verify gate. |
| `plan.py` | `ArchiveAction` / `ArchivePlan` JSON-serialisable, modeled on `cabinet/planner.py`. |
| `config.py`, `platform.py` | `~/attic/config.toml`, default state dirs. |
| `cli.py` | Thin `--confirmed`-gated typer wrappers around `operations`. |

State on disk lives under `~/attic/`:
- `config.toml` — remote name, verify method, `key_fingerprint`
- `plans/plan-*.json` — ArchivePlan output
- `ledgers/{push,restore}-*.jsonl` — append-only audit trail
- `tombstones/<unit>-<ts>.json` — durable breadcrumbs at evict time
- `staging/` — atomic-rename target during dir eviction (data lives here briefly before purge)

## Status

**Phase 1** (current): standalone CLI driving against explicit paths. Test suite: 59 tests, 87% coverage including a hermetic chaos test against `rclone crypt:local` that exercises the real encrypt/decrypt + `.rclonelink` code path. Verified: byte-identical restore of files, symlinks, empty dirs, 0600 modes; refusal on key swap / partial upload / Glacier tier / crash-between-tombstone-and-purge.

**Phase 2** (planned): `candidates.py` reads homing's `~/system/index.json` to auto-score stale ∧ large ∧ ¬regenerable units; triage.md ↔ reconcile loop; `worklist.py` mirroring cabinet's SQLite schema; migrate-skill integration so `attic` slots into the Mode A "leaving" workflow.

## Caveats worth knowing

- **Temp space**: `push` and `evict` round-trip download each unit to a temp dir for verify. Temp size ≈ one unit's size; released per-unit. If `/tmp` is smaller than your largest unit, set `$TMPDIR` or split the plan into smaller units.
- **Crypt password**: stored locally only in `~/.config/rclone/rclone.conf` (obscured, not encrypted). Without that password the bucket is unrecoverable; `init` generates a strong one but you must save it off-box.
- **Glacier / async-retrieval tiers**: `evict` refuses these by default. Pass `--allow-cold-evict` to override, accepting that restore will take hours and cost a retrieval fee.

## License

MIT, same as homing.

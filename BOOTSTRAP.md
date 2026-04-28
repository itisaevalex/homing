# BOOTSTRAP.md — agent entry point

> **You are an agent (Claude Code or similar) that has been started in a freshly cloned `homing` repo. Read this file first.**

This is not the contributor doc. For working on `homing` itself, see [CLAUDE.md](./CLAUDE.md).

This file is for: **someone cloned this repo and started you (the agent) in it because they want help with a migration.** Your job is to figure out what they need and do it.

---

## What this repo gives you

| Path | What it's for |
|---|---|
| `bootstrap.sh` | Install dependencies (chezmoi, age, python deps). Idempotent. Also installs the `migrate` skill into `~/.claude/skills/`. |
| `scripts/bundle.sh` | Generate a migration bundle (dotfiles + secrets + system) from the current machine. |
| `scripts/pack-browsers.sh` | Snapshot Firefox/Chromium/Cursor profiles + portable bookmark/tab/history exports. Aborts if a browser is running. |
| `scripts/pack-personal.sh` | rsync personal-data folders (`~/Documents`, `~/Pictures`, etc.) with cache excludes. |
| `src/homing/` | The `homing` CLI tool — enumerate, classify, draft, validate. |
| `src/cabinet/` | The `cabinet` CLI tool — triage personal-document folders into archive/keep/review. |
| `config/platforms/` | Per-OS YAML config (Linux done, Windows/macOS to come). |
| `skills/migrate/SKILL.md` | The migrate skill — installed by bootstrap into `~/.claude/skills/`. Has more detailed playbooks once you know which mode you're in. |
| `CLAUDE.md` | Contributor doc — for when working on homing itself. |
| `INTENT.md` | Success criteria + non-goals. |

---

## Step −1 — look around before asking

Before interrogating the user, gather priors so you can propose a mode instead of asking cold. Run these checks (cheap, no side effects):

```bash
# Is the user already partway through?
test -d ~/system           && echo "homing has run here before"
test -f ~/system/worklist.sqlite && echo "homing worklist exists"
test -d ~/cabinet          && echo "cabinet has run here before"

# Are dependencies already installed?
command -v chezmoi >/dev/null && echo "chezmoi installed"
command -v age     >/dev/null && echo "age installed"
command -v homing  >/dev/null && echo "homing CLI on PATH"

# Is there a bundle to apply (Mode B signal)?
ls -d ~/migration-bundle-* 2>/dev/null
ls -d /media/*/*migration-bundle* 2>/dev/null
ls -d /run/media/*/*/migration-bundle-* 2>/dev/null
```

Use what you find:

- **`~/migration-bundle-*` or USB-mounted bundle exists** → almost certainly Mode B (arriving). Confirm with the user, don't ask cold.
- **`~/system/` populated AND no bundle around** → likely Mode C (maintenance) or Mode D (exploring). Ask which.
- **No `~/system/`, no bundle, fresh repo clone** → Mode A (leaving) or Mode D (just curious). Ask.

---

## Step 0 — confirm the mode

Once you have priors, frame the question with your guess:

> "Looks like you have a `~/migration-bundle-*` directory here — are you **arriving** at this machine and want me to apply it (Mode B)? Or are you **leaving** (Mode A), running **maintenance** (Mode C), or just **exploring** (Mode D)?"

Mode definitions (one line each, in case the user asks):

- **A — Leaving:** Source machine. Generate a USB bundle to take to a new machine.
- **B — Arriving:** Destination machine. Apply a bundle that's already on disk.
- **C — Maintenance:** Refresh manifests, run cabinet triage, no migration. Most common after the initial migration completes.
- **D — Just exploring:** Map the laptop, no migration, no manifest churn.

---

## Step 1 — bootstrap (every mode)

Run `./bootstrap.sh`. Idempotent. Installs:

- `chezmoi` to `~/.local/bin` (if missing)
- `age` to `~/.local/bin` (if missing, with sha256-pinned download)
- `homing` Python package (editable install, gives you `homing` and `cabinet` CLIs)
- `poppler-utils` via `apt`/`brew` if available (best-effort; needed for cabinet's PDF rendering)
- The `migrate` skill into `~/.claude/skills/migrate/SKILL.md`

After the script finishes, **the `migrate` skill is auto-discovered by Claude Code in subsequent sessions.** If the user starts a fresh `claude` session in any directory after this, the skill loads automatically. SKILL.md contains the detailed per-mode playbooks; this file is the entry, that file is the manual.

If `homing` isn't on PATH after bootstrap, tell the user `export PATH="$HOME/.local/bin:$PATH"` and continue.

---

## Mode A — leaving (generate a bundle)

**Goal: produce a USB-ready bundle the user can carry to the new machine.**

### Happy path

1. Confirm prerequisites: `chezmoi --version`, `age --version`, `homing --help` all work.
2. **Audit secrets.** Search `~/.bashrc`, `~/.zshrc`, `~/.profile` for `export` lines containing `KEY|TOKEN|SECRET|PASSWORD|API`. If any are in plaintext, propose moving them to `~/.config/secrets/api-keys.env` (mode 600) and sourcing from rc. Do NOT proceed with secrets in tracked dotfiles.
3. If `~/.local/share/chezmoi/` is empty, walk the user through `chezmoi init` and adding their critical configs (`.bashrc`, `.gitconfig`, `~/.claude/`, redshift, systemd user units).
4. Optionally run `homing enumerate && homing rules && homing index` so the bundle includes a `~/system/` snapshot (`system.tar.gz`).
5. **(Recommended)** Capture browser profiles + personal data BEFORE the bundle (close all browsers first):
   ```bash
   ./scripts/pack-browsers.sh ~/migration-extras/browsers/
   ./scripts/pack-personal.sh ~/migration-extras/personal/
   ```
   Both chmod 700 their output. Treat as sensitive — encrypted USB only.
6. Generate the bundle: `./scripts/bundle.sh ~/migration-bundle-$(hostname)-$(date +%Y%m%d)`. User will be prompted once for an age passphrase.
7. Verify: `cd <bundle-path> && sha256sum -c MANIFEST.txt`.
8. Tell the user to copy to encrypted USB. Remind them what's NOT in the bundle: SSH/GPG keys, AWS creds (separate encrypted channel).

### When things go wrong

- **`bootstrap.sh` fails on age sha256 mismatch** → the upstream tarball changed or download was corrupted. Ask the user to re-run. If it persists, check `https://github.com/FiloSottile/age/releases/tag/v1.2.1` for the published checksum and update `AGE_SHA256_LINUX_AMD64` in `bootstrap.sh`.
- **`pack-browsers.sh` aborts "browser is running"** → that's the safety check. Have the user close every browser instance (`pkill firefox`, `pkill chromium`, etc.) and re-run. Don't tell them to bypass it; SQLite locks corrupt the snapshot.
- **`bundle.sh` interrupted at age passphrase prompt** → the trap should have shred-deleted the plaintext intermediate. Verify with `ls -la <out>/.secrets-plain.tar.gz` (should be absent). If still there, manually `shred -u` it.

## Mode B — arriving (apply a bundle)

**Goal: restore the user's environment from a bundle on USB.**

### Happy path

1. Locate the bundle. Common spots: `/media/<user>/<usb-label>/migration-bundle-*`, `/run/media/<user>/.../migration-bundle-*`, `~/Downloads/`, or already at `~/`.
2. Verify integrity: `cd <bundle-path> && sha256sum -c MANIFEST.txt`. All checksums must pass before you proceed.
3. Read the bundle's `README.md`. Confirm with the user they recognize the source hostname + timestamp.
4. Run `bash setup.sh` from inside the bundle directory. They'll be prompted once for the age passphrase.
5. **Do not source the new bashrc in the agent's shell.** Tell the user to open a fresh terminal so secrets and PATH load cleanly.
6. Verification pass (in a fresh shell):
   ```bash
   grep -c api-keys.env ~/.bashrc           # should be ≥ 1
   ls -l ~/.config/secrets/api-keys.env     # should be mode 600
   ls ~/.claude/agents/                     # should contain user's custom agents
   bash -ic 'env | grep -cE "API_KEY|SECRET|TOKEN"'  # adjust pattern to match your service names
   ```
   The env-var count is **non-zero is the only requirement** — ask the user how many vars they expect to see, then compare.
7. Walk through what's NOT in the bundle:
   - SSH key generation + adding to GitHub
   - AWS profile setup (`aws configure`)
   - Tailscale (`tailscale up`)
   - GH CLI auth (`gh auth login`)

### When things go wrong

- **`sha256sum -c MANIFEST.txt` reports a mismatch** → STOP. The bundle is corrupt or tampered. Don't run `setup.sh`. Ask the user to re-copy from the source USB or re-generate.
- **Wrong age passphrase** → `setup.sh` aborts before any state mutation. Re-run.
- **`secrets.tar.gz.age` decrypts but tarball extract fails** → likely truncated transfer. Re-copy from USB.
- **`systemctl --user` errors** → expected on systems without systemd-user (e.g. some minimal Ubuntu/Debian containers). Non-fatal.
- **`chezmoi init --apply=false` fails** → non-fatal. The dotfiles already extracted via `tar`; chezmoi is a follow-up tool, not load-bearing.

## Mode C — maintenance

**Goal: refresh the existing system on this machine without doing a migration.**

### Refresh project legibility (homing)

```bash
homing enumerate          # walk $HOME, update unit list
homing rules              # deterministic rule classification
```

If `homing rules` leaves any unknowns, run the LLM cascade via the orchestrator (no API key needed):

```bash
homing classify --via-orchestrator
# This emits ~/system/batches/batch-NNN.json per unknown unit.
```

Then YOU (the orchestrator) loop the batches — see "**fan-out pattern**" below. After all subagents finish, ingest:

```bash
homing ingest-findings
```

### Refresh manifests for new projects

```bash
homing index              # aggregate frontmatter → ~/system/index.json
homing query stale        # which projects haven't been touched in >90 days?
```

If new projects appeared since last run, draft AGENT.md for each:

```bash
homing draft <name> --via-orchestrator
# emits ~/system/draft-requests/<name>.json
```

Loop the draft requests, then optionally validate:

```bash
homing validate --all --via-orchestrator
# emits ~/system/validate-requests/<name>.json per AGENT.md
homing ingest-validations
```

### Triage a personal-document folder (cabinet)

Full cycle on `~/Documents` (or any chosen folder):

```bash
cabinet scan ~/Documents --output-dir ~/cabinet
# Show user the homogeneity verdicts. Confirm scope.

cabinet classify --output-dir ~/cabinet --via-orchestrator
# emits ~/cabinet/batches/batch-NNN.json. Loop, fan out subagents,
# write batch-NNN.results.json files, then:
cabinet ingest-findings --output-dir ~/cabinet

cabinet triage --output-dir ~/cabinet
# writes ~/cabinet/triage.md — tell the user to open it in an editor and
# mark checkboxes. WAIT for them to come back. Do not auto-open.

cabinet reconcile --output-dir ~/cabinet
# reads the marked-up triage.md back into decisions

cabinet plan --output-dir ~/cabinet
# materializes the plan as ~/cabinet/plan-<ts>.json. Show the summary
# (count of moves, total bytes, top-N largest items). Read the first 10
# actions verbatim out loud before approval.

cabinet apply --confirmed --output-dir ~/cabinet
# Only after explicit "yes". Records an undo ledger.

# If the user regrets anything later:
cabinet undo <ledger-id>
# Reverses the moves, verifying byte-identical content before each reversal.
```

### When things go wrong

- **`homing rules` returns 0 classified, all unknown** → check that `config/platforms/<os>.yaml` exists for the user's OS. If not (Windows/macOS), maintenance mode is degraded — tell the user.
- **`homing classify --via-orchestrator` writes 0 batches** → all units already classified by rules. The empty-batches output is intentional ("0 unknowns — proceed to triage"). Don't loop forever.
- **`cabinet apply` aborts mid-plan** → `_emergency_rollback` runs automatically; completed moves are reversed. Show the user the ledger output and the failure reason. Their data is still intact.
- **`cabinet undo` reports "dest content changed since apply"** → the user (or another tool) modified a file after the move. Refuse to clobber — surface the failure list, ask the user how to proceed file-by-file.

## Mode D — just exploring

**Goal: produce structured information about the laptop, no migration, no manifest churn.**

```bash
homing enumerate
homing summary             # ~/system/overview.md — readable in 5 minutes, no LLM
homing rules
homing index
homing query list
```

Show the user `~/system/overview.md`. Ask whether they want to draft AGENT.md for any specific project (`homing draft <name> --via-orchestrator`). Don't run `--all` without explicit consent — that's a token-spend choice.

---

## --via-orchestrator fan-out pattern

`homing draft --via-orchestrator <name>`, `homing validate --via-orchestrator [--all|<name>]`, and `cabinet classify --via-orchestrator` don't call Anthropic directly — they write JSON request bundles to disk:

| Command | Bundle directory | Result location |
|---|---|---|
| `homing draft --via-orchestrator <name>` | `~/system/draft-requests/<name>.json` | written by sub-agent to `target_path` field |
| `homing validate --via-orchestrator` | `~/system/validate-requests/<name>.json` | written by sub-agent to `result_path` field |
| `cabinet classify --via-orchestrator` | `~/cabinet/batches/batch-NNN.json` | sub-agent writes `~/cabinet/batches/batch-NNN.results.json` |

**You (the orchestrator) handle each request:**

1. List the bundle dir: `ls ~/system/draft-requests/*.json` (or the relevant path).
2. For each file: read it, spawn a sub-agent with the embedded `user_message` (and `schema` for drafts). The sub-agent writes its output to the request's `target_path` / `result_path`.
3. **Fan out in parallel** — multiple Agent calls in one message, since each request is independent. Cap parallelism at 7 (typical session subagent budget).
4. After all sub-agents complete, run the matching ingest command:
   - `homing ingest-validations` for validate
   - `cabinet ingest-findings --output-dir ~/cabinet` for cabinet classify
   - `homing draft` writes its output directly to the target file, no separate ingest needed

Loop the bundle dir, don't single-shot. Each request is independent.

---

## Things you should always do, regardless of mode

- **Never push the user's `~/.local/share/chezmoi/` to a remote.** That's by design (see CLAUDE.md hard rules).
- **Never put secrets in chezmoi.** Only in `~/.config/secrets/` or an external secret store.
- **Never modify `$HOME` files outside chezmoi/secrets without telling the user.** `bundle.sh` is read-only against `$HOME`. `setup.sh` writes to `$HOME` only via documented paths.
- **Citations rule applies.** If you tell the user "I found X in your config," cite the file path.
- **One question at a time.** Don't dump a 10-question checklist; surface decisions as you reach them.

## When you're done

Whichever mode you ran, end the session by:

1. Summarizing what you did in one paragraph.
2. Listing what's still on the user's plate (separate-channel things: SSH keys, AWS creds, etc.).
3. Mentioning that the next time they clone this repo on any machine, the same flow works — and that the `migrate` skill is now installed in `~/.claude/skills/`, so any Claude Code session on this machine can pick up where you left off.

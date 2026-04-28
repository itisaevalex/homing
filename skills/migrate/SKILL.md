---
name: migrate
description: Mech suit for laptop migration and personal-document triage. Use when the user asks about migrating to/from a laptop, applying a migration bundle, triaging messy folders, or running homing/cabinet workflows. Coordinates homing (dev environment) + cabinet (personal documents) safely. Always defaults to read-only / dry-run modes; never moves user data without explicit confirmed approval.
---

# migrate — orchestrator playbook

This skill is the user's mech suit. The user does not run a `migrate` command — *I* run homing and cabinet on their behalf in the right order, with the right safety gates.

## Step 0 — establish ground truth

Before doing anything, surface the safety contract out loud:

> "I'll run homing (dev environment) and cabinet (personal documents) for this. Defaults are read-only and dry-run. I will never move or delete a file without showing you the planned actions and getting explicit approval. The chaos test in cabinet's repo proves the apply→undo round-trip is byte-identical."

Then ask which **mode** the user is in:

- **A — Source machine, preparing to leave** (generate bundle + cabinet plan)
- **B — Destination machine, applying a bundle** (restore dev env + execute cabinet plan)
- **C — Mid-life maintenance** (re-scan, re-triage, drift detection)
- **D — Just exploring** (no actions, just look around)

Don't combine modes. Each has a runbook below. Pick one.

---

## Repo discovery + bootstrap

Both tools live in a single repo at `git@github.com:itisaevalex/homing.git`. After installation you get two CLIs on PATH: `homing` and `cabinet`.

On any machine, before the workflow:

```bash
HOMING=~/Projects/homing
[ -d "$HOMING" ] || git clone git@github.com:itisaevalex/homing.git "$HOMING"
(cd "$HOMING" && ./bootstrap.sh)
```

`bootstrap.sh` is idempotent. It installs chezmoi, age, the python package (gives you both `homing` and `cabinet` CLIs), poppler-utils via apt for pdf2image (cabinet's PDF rendering), and the `migrate` skill into `~/.claude/skills/`.

`$CABINET` and `$HOMING` are interchangeable below — they refer to the same checkout. Older versions of this skill expected two separate repos; that's no longer the case.

---

## Mode A — leaving (source machine)

Goal: produce a USB-ready bundle that captures the dev environment AND a triaged plan for personal docs.

### A.1 — dev environment (homing)

```bash
cd "$HOMING"
homing enumerate                    # Phase A — deterministic walk of $HOME
homing rules                        # Phase C — deterministic rule plugins
homing index                        # Phase G — aggregate frontmatter → JSON
```

LLM-touching phases (no API key needed when inside Claude Code):
```bash
homing summary                                  # Phase B — readable overview, deterministic
homing draft <name> --via-orchestrator          # Phase E — emits ~/system/draft-requests/<name>.json
homing validate --all --via-orchestrator        # Phase F — emits ~/system/validate-requests/<name>.json per AGENT.md
homing ingest-validations                       # persist results to worklist
```

**Fan-out pattern for `--via-orchestrator` (applies to every command above + cabinet classify):**

The CLI doesn't single-shot to Anthropic; it writes JSON request bundles. YOU loop:

1. List the bundle dir: e.g. `ls ~/system/draft-requests/*.json` or `~/system/validate-requests/*.json` or `~/cabinet/batches/batch-*.json`.
2. For each file: read it, spawn a sub-agent with the embedded `user_message` (and `schema` for drafts). The sub-agent writes its output to the request's `target_path` / `result_path` field.
3. Fan out in parallel — multiple Agent calls in one message, since each request is independent. Cap parallelism to the session's per-window subagent budget (typically 7).
4. After all sub-agents complete, run the matching `ingest-*` command to persist findings to the worklist.

Standalone (with `ANTHROPIC_API_KEY`): same commands minus `--via-orchestrator` and the CLI calls Anthropic itself.

### A.2 — personal documents (cabinet)

```bash
cd "$CABINET"
cabinet scan ~/Documents ~/Pictures ~/Downloads --output-dir ~/cabinet
```

Show the user the scan summary (folder count, file count, homogeneity verdicts). **Confirm the scope before classifying.**

```bash
cabinet classify --output-dir ~/cabinet --via-orchestrator
# This emits ~/cabinet/batches/batch-NNN.json for each remaining unknown unit.
# YOU (the orchestrator) fan out subagents in parallel, one per batch, and write
# results to ~/cabinet/batches/batch-NNN.results.json. Then:
cabinet ingest-findings --output-dir ~/cabinet
# This parses the results files, writes findings to the worklist.
# No ANTHROPIC_API_KEY needed — subagents use Claude Code's session auth.
cabinet triage --output-dir ~/cabinet    # writes ~/cabinet/triage.md
```

Tell the user to open `~/cabinet/triage.md` in their editor and mark checkboxes. Do NOT auto-open it. Wait for them to come back.

```bash
cabinet reconcile --output-dir ~/cabinet
cabinet plan --output-dir ~/cabinet
```

Show the plan summary (count of moves, total bytes, top-N largest items). **Read the first 10 actions verbatim out loud** before approval. If anything looks wrong, surface it.

```bash
cabinet apply --confirmed --output-dir ~/cabinet
```

Only after explicit "yes". Show the undo ledger ID. If the user later regrets anything: `cabinet undo <id>`.

### A.2.5 — browser profiles + personal-data piles (optional but usually wanted)

Run BEFORE bundle generation, after asking the user to close every browser:

```bash
cd "$HOMING"
./scripts/pack-browsers.sh ~/migration-extras/browsers/
./scripts/pack-personal.sh ~/migration-extras/personal/
```

`pack-browsers.sh`:
- Aborts (not just warns) if it detects a running browser — open profiles produce inconsistent SQLite snapshots that may be unrecoverable.
- Captures Firefox, Chromium-family, and Cursor profile trees (cookies, sessions, encrypted password DB included).
- Emits portable fallbacks per profile: `<profile>.bookmarks.html` (Firefox) / `.bookmarks.json` (Chromium), `<profile>.tabs.json`, `<profile>.history.json`. Use these on the target machine if profile-tree restoration fails for any reason.
- Output dir is `chmod 700` from creation; treat it as you would your password vault. Move to encrypted storage before leaving the machine.

`pack-personal.sh`:
- Bulk byte-mover. Default targets are `~/Documents`, `~/Pictures`, `~/Music`, `~/Videos`, `~/Desktop`.
- Excludes caches, `node_modules`, `.venv`, `__pycache__`, etc.
- After cabinet has triaged stuff into archive/review piles, point this at the cleaned tree (`~/cabinet-archive/`) instead of raw `~/Documents`.

These outputs sit alongside the homing bundle. They are NOT included in `bundle.sh`'s output by design — large + sensitive + better stored on a separate encrypted volume.

### A.3 — bundle generation

```bash
cd "$HOMING"
./scripts/bundle.sh ~/migration-bundle-$(hostname)-$(date +%Y%m%d)
```

The bundle includes:
- `dotfiles.tar.gz` — chezmoi archive (bashrc, gitconfig, ~/.claude, redshift, systemd units)
- `secrets.tar.gz.age` — age-encrypted ~/.config/secrets (passphrase or keyfile)
- `system.tar.gz` — homing's ~/system/ snapshot
- `setup.sh` — target-machine bootstrap
- `MANIFEST.txt` — sha256 checksums

The cabinet output (`~/cabinet/`) is NOT in the bundle by default. If the user wants the post-triage personal docs to come along, they're now organized in `~/cabinet-archive/` (cabinet's archive root) — copy that to a separate USB or external drive.

### A.4 — what's NOT in the bundle (separate channels)

Always remind the user:
- `~/.ssh/id_*` — SSH keys
- `~/.gnupg/` — GPG keys
- `~/.aws/credentials` — AWS profiles
- bundle passphrase (the age phrase used to encrypt `secrets.tar.gz.age`) — keep in your password manager, or split with the bundle USB on a second device
- Tailscale auth key (from password manager)

---

## Mode B — arriving (destination machine)

Goal: restore the dev environment from a bundle, then optionally execute cabinet's plan against the personal documents (if the user brought them).

### B.1 — apply the homing bundle

```bash
cd ~/path/to/migration-bundle-*
sha256sum -c MANIFEST.txt           # integrity check first
bash setup.sh                       # prompts once for the age passphrase
```

After setup completes, **do not auto-source the new bashrc in the current session** — tell the user to open a fresh shell. Verify in fresh shell:

```bash
# Non-zero is the only requirement. Don't pin a magic number — ask the user how
# many env vars they expect, then compare. Counts depend on what was in
# secrets/api-keys.env on the source.
env | grep -cE 'API_KEY|SECRET|TOKEN'  # adjust pattern to match your service names
ls ~/.claude/agents | wc -l                                # should match source count
```

### B.2 — clone the tool repos on the new machine

The bundle includes ~/.claude/ but NOT the homing repo itself. Clone it:

```bash
mkdir -p ~/Projects
cd ~/Projects
git clone git@github.com:itisaevalex/homing.git
(cd homing && ./bootstrap.sh)
```

This single repo gives you both `homing` and `cabinet` CLIs.

### B.3 — apply cabinet's plan (if the user brought it)

If the user copied `~/cabinet-archive/` to the new machine, the post-triage personal docs are already organized — they just need to move them from the USB to `$HOME`. No re-run of cabinet needed.

If they want to re-triage on the new machine (different decisions in a clean environment), repeat Mode A.2 from scratch.

### B.4 — separate-channel restoration

```bash
# SSH
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cp /media/usb/ssh/id_* ~/.ssh/
chmod 600 ~/.ssh/id_*

# AWS profiles
aws configure --profile <your-aws-profile>
# (and any others)

# Tailscale
sudo tailscale up --auth-key=<your-tailscale-auth-key>

# GitHub CLI
gh auth login
```

Walk the user through each one. Don't dump them all at once.

---

## Mode C — mid-life maintenance

Goal: re-scan and detect drift on a machine that's been alive for a while.

```bash
homing enumerate                    # re-walk $HOME
homing rules
homing index                        # produces a fresh index.json
diff <(jq . old-index.json) <(jq . ~/system/index.json)   # see what changed
```

For cabinet:
```bash
cabinet scan <paths> --output-dir ~/cabinet
# Compare scan_result.json against the previous run if you snapshotted it
```

This is the "weekly hygiene" pattern. Catches:
- New projects you forgot you started
- Files growing in unexpected places
- Things that should have been triaged but weren't

---

## Mode D — just exploring

```bash
homing enumerate
homing summary           # human-readable overview
homing query active      # what projects are alive?
homing query stale       # what's > 90 days untouched?

cabinet scan ~/Pictures
# inspects + samples; no actions; ~/cabinet/scan_result.json is the artifact
```

---

## Always-applicable safety rules

These apply to ALL modes. Never deviate.

### 1. Source is sacred

Both tools' design contracts say: read-only against `$HOME` outside of explicit `--apply` flows. I never modify user files as a side effect of inspection. If the user asks me to "just go and do it," I still show the plan first.

### 2. Citations required

When I claim something — "this is your CV folder" or "these 4 files are duplicates" — I must point at the evidence (filename pattern, EXIF data, content hash). The cabinet classifier does this automatically; my job is to read those citations aloud when surfacing decisions.

### 3. One question at a time

The user has limited bandwidth for batched approvals. I ask about specific decisions, not a 20-question intake form.

### 4. Cost transparency

Before kicking off anything that spends API tokens (homing draft on N projects, cabinet classify on M units), state the estimated cost. The user has authorized "no token budget" once; that doesn't mean they want surprise $50 runs.

### 5. Confirm before destructive operations

`cabinet apply` / `homing draft` (overwrites a `*.proposed.md`) / any action that modifies user state requires explicit "yes" in the same turn — not implied from earlier permissive language.

### 6. Resume gracefully

Both tools have SQLite worklists that persist state. If a session is interrupted mid-run, the next session can resume from `units_by_status('discovered')` etc. I should always check worklist state before re-running phases — re-enumerating is wasted work if the previous run is already there.

### 7. Verify after each phase

Don't blindly chain `enumerate → rules → classify → ...`. Between phases, surface a short status to the user and confirm. The chain breaks expensively if something earlier was wrong.

---

## Live-test safe mode (no API key)

If the user wants to exercise the pipeline without any LLM spend, use this restricted toolset:

**homing (deterministic only):**
- ✓ enumerate, summary, rules, index, query

**cabinet (deterministic only):**
- ✓ scan
- ✓ classify (rules cascade only — LLM fallback fails clearly without key, that's fine)
- ✓ triage (renders what was classified; unclassified units appear with `class: unknown`)
- ✓ plan (built from rule-classifications + user decisions)
- ✓ apply (executes the plan — independent of LLM)
- ✓ undo (the chaos-tested round-trip)

This proves: enumeration is correct, deterministic rules fire correctly, the apply→undo round-trip is byte-identical, the undo ledger is recoverable.

What it does NOT prove: LLM classification accuracy, multimodal vision pipeline, fresh-agent validation. Those require an API key.

---

## Quick command reference

```
homing enumerate / summary / rules / index / query / draft / validate
homing scripts/bundle.sh <out-dir>

cabinet scan / classify / triage / reconcile / plan / apply / undo / query
cabinet bootstrap.sh

chezmoi add / apply / status / diff
age -p / age -d
```

If I get confused mid-flow, re-read the relevant Mode section above. Each is a runbook, not a guideline.

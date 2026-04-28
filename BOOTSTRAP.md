# BOOTSTRAP.md — agent entry point

> **You are an agent (Claude Code or similar) that has been started in a freshly cloned `homing` repo. Read this file first.**

This is not the contributor doc. For working on `homing` itself, see [CLAUDE.md](./CLAUDE.md).

This file is for: **someone cloned this repo and started you (the agent) in it because they want help with a migration.** Your job is to figure out what they need and do it.

---

## Step 0 — find out which mode you're in

Ask the user **once**, plainly:

> "Are you (a) **leaving** this machine — generating a migration bundle to take to a new one, (b) **arriving** at this machine — applying a bundle that's already on disk, or (c) **just exploring** — running homing to map the laptop without migrating?"

Don't guess. Each mode has a different first action.

---

## Mode A — leaving (generate a bundle)

**You're on the source machine. Goal: produce a USB-ready bundle the user can carry to the new machine.**

1. Run `./bootstrap.sh` to install dependencies (chezmoi, age, python deps). Idempotent.
2. Confirm prerequisites:
   - `chezmoi --version` works
   - `age --version` works
   - `homing --help` works
3. If `~/.local/share/chezmoi/` is empty (chezmoi never initialized), ask the user whether they already have a chezmoi-managed dotfile setup. If not, walk them through `chezmoi init` and adding their critical configs (`.bashrc`, `.gitconfig`, `~/.claude/`, etc.) — see `docs/workflows/migration.md` for what to include.
4. **Audit secrets.** Search `~/.bashrc`, `~/.zshrc`, `~/.profile` for export lines containing `KEY|TOKEN|SECRET|PASSWORD|API`. If any are found in plaintext, propose moving them to `~/.config/secrets/api-keys.env` (mode 600) and source from rc file. Do NOT proceed to bundle generation with secrets still in tracked dotfiles.
5. Optionally: run `homing enumerate && homing rules && homing index` to capture the source machine's project state. This goes in the bundle as `system.tar.gz`.
6. Run `./scripts/bundle.sh <output-path>`. Default output is `~/migration-bundle-<timestamp>/`. The user will be prompted for an age passphrase to encrypt the secrets.
7. Verify the bundle: `cd <output-path> && sha256sum -c MANIFEST.txt`. All checksums should pass.
8. Tell the user to copy the directory to their encrypted USB and remind them what the bundle does NOT contain (SSH/GPG keys, AWS creds, browser profile — those need separate channels).

## Mode B — arriving (apply a bundle)

**You're on the destination machine. Goal: restore the user's environment from a bundle on USB.**

1. Ask: where is the bundle directory? Common answers: `/media/<user>/<usb-label>/migration-bundle-<ts>`, `~/Downloads/...`, or already copied to `~/`.
2. Verify integrity before doing anything: `cd <bundle-path> && sha256sum -c MANIFEST.txt`.
3. Read the bundle's `README.md`. Confirm with the user that they recognize the source machine + timestamp.
4. Run `bash setup.sh` from inside the bundle directory. They will be prompted once for the age passphrase.
5. After setup completes, **do not auto-source the new bashrc in the current session** — tell the user to open a fresh shell. (Sourcing in the agent's shell can produce confusing state.)
6. Run a verification pass:
   - `cat ~/.bashrc | grep -c api-keys.env` — should be ≥ 1
   - `ls ~/.config/secrets/api-keys.env` — should exist with mode 600
   - `ls ~/.claude/agents/` — should contain the user's custom agents
   - In a fresh shell: `bash -ic 'env | grep -E "STRIPE|SUPABASE" | wc -l'` — should be ≥ 6
7. Remind the user about the things NOT in the bundle and offer to walk through each (SSH key gen + add to GitHub, AWS profile setup, Tailscale `up`, etc.).

## Mode C — just exploring

**Goal: produce structured information about the laptop, no migration.**

1. `./bootstrap.sh`
2. `homing enumerate && homing summary && homing rules && homing index`
3. Show the user `~/system/overview.md` and the `homing query list` output.
4. Ask whether they want to run `homing draft` on any specific project (LLM-touching, costs tokens).

---

## Things you should always do, regardless of mode

- **Never push the user's `~/.local/share/chezmoi/` to a remote.** That's by design (see CLAUDE.md hard rules).
- **Never put secrets in chezmoi.** Only in `~/.config/secrets/` or an external secret store.
- **Never modify `$HOME` files outside chezmoi/secrets without telling the user.** `bundle.sh` is read-only against `$HOME`. `setup.sh` writes to `$HOME` only via documented paths.
- **Citations rule applies.** If you tell the user "I found X in your config," cite the file path.
- **One question at a time.** Don't dump a 10-question checklist; surface decisions as you reach them.

## What this repo gives you

| File | What it's for |
|---|---|
| `bootstrap.sh` | Install dependencies (chezmoi, age, python deps). Idempotent. |
| `scripts/bundle.sh` | Generate a migration bundle from the current machine. |
| `src/homing/` | The `homing` CLI tool — enumerate, classify, draft, validate. |
| `config/platforms/` | Per-OS YAML config (Linux done, Windows/macOS to come). |
| `docs/workflows/` | Detailed runbooks for migration / first-run / Windows. |
| `CLAUDE.md` | Contributor doc — for when working on homing itself. |
| `INTENT.md` | Success criteria + non-goals. |

## When you're done

Whichever mode you ran, end the session by:

1. Summarizing what you did in one paragraph.
2. Listing what's still on the user's plate (separate-channel things: SSH keys, AWS creds, etc.).
3. Mentioning that the next time they clone this repo on any machine, the same flow works.

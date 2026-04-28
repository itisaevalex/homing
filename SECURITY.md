# Security Policy

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report them through one of these channels:

1. **GitHub Security Advisories** — preferred. Go to the [Security tab](https://github.com/itisaevalex/homing/security/advisories/new) of this repository and open a private advisory. The maintainer will acknowledge within 72 hours.
2. **Email** — if you prefer, email the maintainer directly at the address in the git log. Use "homing security" in the subject line. PGP is welcome; use the key fingerprint in the maintainer's GitHub profile if one is listed.

Please include:
- A description of the vulnerability and the affected component.
- Steps to reproduce (a minimal reproducer helps enormously).
- The potential impact in your assessment.
- Whether you have a proposed fix or patch.

We aim to issue a fix or a clear mitigation plan within 14 days of a confirmed critical report.

## Threat model

Understanding what homing does and doesn't do is essential context for any security analysis.

### What homing does

| Operation | Scope | Direction |
|-----------|-------|-----------|
| `homing enumerate / summary / rules / index / query` | Reads `$HOME` (or configured root) | Read-only |
| `homing classify / draft / validate` | Reads `$HOME`; writes only to `~/system/` | Read source, write output |
| `cabinet scan / classify / triage / reconcile / plan` | Reads configured folders; writes to `~/cabinet/` | Read source, write output |
| `cabinet apply` | Moves files within the local filesystem | Write, with undo ledger |
| `bundle.sh` | Reads chezmoi archive + `~/.config/secrets`; age-encrypts | Read-only against `$HOME`; writes to the bundle output dir |
| `setup.sh` / `bootstrap.sh` | Installs tools to `~/.local/bin`; writes `~/system/` + `~/.config/secrets` on the target | Write to controlled paths only |

### What homing never does

- Makes network requests during classification, drafting, or validation. The only network calls are: chezmoi install in `bootstrap.sh`, age binary download in `bootstrap.sh` (sha256-pinned), and Anthropic API calls in standalone (non-`--via-orchestrator`) mode if `ANTHROPIC_API_KEY` is set.
- Writes to `$HOME` source files during enumeration, classification, or drafting. Source is read-only. Any future opt-in write-back feature would be a separate subcommand with per-file confirmation.
- Sends personal files to any server. LLM classification in `--via-orchestrator` mode passes only metadata and file samples to the Claude Code session's own model inference — not to an external storage service.

### Secrets handling

- `~/.config/secrets/` holds plaintext secrets on the source machine. Mode 600 files under a mode 700 directory.
- `bundle.sh` tarballs the secrets dir, age-encrypts it with a user-chosen passphrase, and shreds the plaintext intermediate using a `trap` on EXIT, INT, TERM, and HUP signals.
- The plaintext intermediate (`$OUT/.secrets-plain.tar.gz`) must never persist after `bundle.sh` exits. **Any deviation — for example, an early exit path that bypasses the trap — is a vulnerability worth reporting.**
- `setup.sh` (inside a migration bundle) decrypts the secrets into a per-user `mktemp` directory (mode 700), extracts to `~/.config/secrets`, then shreds the temp plaintext using the same trap pattern.
- Age is downloaded with a pinned sha256 checksum in `bootstrap.sh`. A mismatch aborts the install.

### What's in scope

| In scope | Notes |
|----------|-------|
| Plaintext intermediate not shredded on interruption | The `trap` pattern in `bundle.sh` and `setup.sh` |
| Secrets written to wrong path / wrong permission | `~/.config/secrets` should be 700/600 recursively |
| Network exfiltration of user files | Any case where user content leaves the machine unexpectedly |
| Privilege escalation via `bootstrap.sh` | The script requests `sudo` only for `apt-get install poppler-utils`; any other elevation is a bug |
| Injection via malicious filenames or YAML | Platform config and content taxonomy are parsed from disk |
| Age binary substitution | sha256 mismatch should abort; a bypass would be a vulnerability |
| Insecure temp file handling | Any case where secrets land in world-readable `/tmp` |

### What's out of scope

| Out of scope | Reason |
|--------------|--------|
| Secrets stored in chezmoi source | By design: chezmoi manages dotfiles, not secrets. Secrets go in `~/.config/secrets/`, not chezmoi. |
| Browser password theft | `pack-browsers.sh` copies the browser's encrypted password DB; decryption requires the OS keyring, which is not accessed by homing. |
| Physical access to the USB bundle | Age passphrase is the protection boundary. Losing the USB without a passphrase is not a homing vulnerability. |
| LLM prompt injection via file content | The LLM is used only by the local Claude Code session. Adversarial file content that manipulates classification output is an interesting research topic but not a homing security issue. |
| Coverage of third-party tools | chezmoi, age, poppler, Anthropic API — their vulnerabilities are theirs. We pin age and recommend staying current on the others. |

## Supported versions

| Version | Supported |
|---------|-----------|
| main branch (v0.1.x) | Yes |
| Older commits | No — please update to main |

## Disclosure policy

We follow coordinated disclosure. We ask that you:
- Give us reasonable time to investigate and patch before public disclosure.
- Do not access, modify, or exfiltrate data beyond what is necessary to demonstrate the issue.

We will credit you in the release notes unless you prefer to remain anonymous.

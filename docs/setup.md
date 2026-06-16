# bakar setup

Prepare the host once, before your first `bakar build`. `setup` profiles the
machine, turns the host-environment `bakar doctor` findings into applied
remediations, shows you an auditable plan, and records what it applied into the
global `[host]` config so `doctor` later verifies against it.

This is a host-level command (once per machine). It is distinct from
[`bakar init`](init.md), which scaffolds a single workspace (`.bakar.toml`,
`nxp/`/`ti/` dirs) and touches nothing on the machine itself.

## Synopsis

```text
bakar setup [OPTIONS]
```

## Options

| Flag | Description |
|------|-------------|
| `--dry-run` | Print the host profile and the full generated script, mutate nothing |
| `--yes` | Skip the confirm gate after a passwordless-sudo precheck |
| `--git-email` | Global git identity email to set (`git config --global user.email`) |
| `--git-name` | Global git identity name to set (`git config --global user.name`) |

## What it does

1. **Profiles the host** - CPU count, available memory, free disk, distro
   (`ID`/`ID_LIKE` from `/etc/os-release`) and its package manager, docker group
   membership, whether the docker binary is installed, and the live sysctl/ulimit
   knobs it may remediate. Profiling is read-only.
2. **Builds an auditable plan** - maps each host-environment `doctor` check to a
   remediation action and drops every action that is already satisfied, so a
   prepared host yields an empty plan.
3. **Applies unprivileged actions inline** - `uv tool install kas`, `docker pull`
   of the resolved container image, `git config --global` identity, and the
   sstate/dl/ccache `mkdir -p` under `$HOME` run directly in your user context, no
   sudo.
4. **Applies privileged actions through a single sudo** - every root operation is
   assembled into an auditable `set -euo pipefail` script and piped via stdin to
   `sudo bash -s` after one confirmation. The script is never written to disk;
   use `--dry-run` to inspect it before running. There is no per-action sudo and
   no `curl`-piped-to-shell.
5. **Records applied values into global `[host]`** - the host-knob values it
   applied are written to the `[host]` section of `~/.config/bakar/config.toml`
   via `bakar settings` (`host.inotify_instances`, `host.inotify_watches`,
   `host.swappiness_max`, `host.nofile_soft`). It never writes a workspace
   `.bakar.toml`; pin host expectations in-repo only by editing it yourself.

## Privileged actions

These land in the single confirmed script:

| Action | What it does |
|--------|--------------|
| sysctl | Writes `/etc/sysctl.d/99-bakar.conf` (a removable drop-in, never `/etc/sysctl.conf`) and runs `sysctl --system` |
| docker `daemon.json` | Merges `default-ulimits.nofile` and `storage-driver: overlay2` via a parse-validated `python3` round-trip, after backing up `daemon.json.bakar.bak`; pre-existing keys are preserved |
| docker service | `systemctl enable --now docker` |
| docker group | `usermod -aG docker $USER`, with a warning that the change needs a new login session |

## Advisory only

These are reported in the plan but never auto-applied:

- Low available memory.
- Low free disk.
- An unsupported workspace filesystem.
- The container's Python version.
- Docker engine install - when docker is absent, `setup` prints the official
  per-distro install command as text and contributes no docker-engine action (and
  no docker-dependent action).

## Idempotency

Run `setup` again on a prepared host and every action reports as already
satisfied, so the plan is empty. It prints a no-op message, applies nothing, and
writes no new backup or sysctl file.

## Examples

```bash
# Inspect the profile and the exact script without touching anything
bakar setup --dry-run

# Prepare the host, confirming the single sudo escalation interactively
bakar setup --git-email you@example.com --git-name "Your Name"

# Non-interactive: requires passwordless sudo, fails fast if not available
bakar setup --yes --git-email you@example.com --git-name "Your Name"
```

`--yes` runs `sudo -n true` first. When sudo would need a password it exits
non-zero with a message naming the missing passwordless-sudo precondition rather
than hanging on a prompt.

## After setup

Run `bakar doctor` to confirm the remediated host-environment checks (sysctl,
docker-ulimits, docker-storage-driver, git-global-config, container-image) flip
to PASS. `doctor` verifies the machine against the `[host]` values `setup` wrote.

## See also

- [doctor.md](doctor.md) - the read-only checks `setup` remediates
- [settings.md](settings.md) - the `host.*` keys `setup` persists to global config
- [init.md](init.md) - the per-workspace scaffolder (run after `setup`)
- [configuration.md](configuration.md) - config resolution and the `[host]` precedence chain

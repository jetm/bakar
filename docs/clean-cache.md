# bakar clean-cache

Prune stale sstate-cache and ccache entries by age to reclaim disk space.

## Synopsis

```text
bakar clean-cache [OPTIONS]
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--older-than` | `30` | Remove entries older than N days (applies to both caches) |
| `--sstate-dir` | - | Override the SSTATE_DIR path |
| `--ccache-dir` | - | Override the ccache directory |
| `--sstate` / `--no-sstate` | on | Prune (or skip) the sstate cache |
| `--ccache` / `--no-ccache` | on | Evict (or skip) the ccache |
| `--yes`, `-y` | - | Skip the confirmation prompt (for scripting) |
| `--dry-run`, `-n` | - | Scan and report without prompting or deleting |

## Behavior

By default the command prunes both caches. It reports what each would remove,
then prompts once before acting:

```text
SSTATE_DIR : /home/user/yocto-cache/sstate
Time basis : atime (last read)
Threshold  : 30 days
sstate     : 1,247 files older than 30 days, totalling 14.3 GiB
ccache     : /home/user/repos/bsp/ccache (9.0 GiB)

Proceed to delete 1,247 sstate files (14.3 GiB) and evict ccache entries older than 30 days? [y/N]:
```

Restrict to one cache with `--no-ccache` (sstate only) or `--no-sstate`
(ccache only).

## sstate vs ccache pruning

The two caches are pruned differently because they have different on-disk
contracts:

- **sstate** is a flat directory of self-contained archives. `clean-cache` uses
  a two-phase approach: stale files are first renamed into a `.bakar-gc-<pid>/`
  staging directory created inside the sstate root (so the rename is atomic and
  stays on the same filesystem), then the staging tree is removed wholesale. This
  means a concurrent build can never observe a half-deleted sstate entry. If
  interrupted between the rename and the rmtree, a `.bakar-gc-<pid>/` directory
  is left behind inside the sstate root; remove it manually to reclaim the space.
  Emptied parent directories are pruned after deletion.
- **ccache** keeps its own index, manifests, and statistics. Deleting files by
  hand would corrupt that bookkeeping, so `clean-cache` delegates to ccache:
  it runs `ccache --evict-older-than Nd` against the resolved cache directory.

## SSTATE_DIR resolution

1. `--sstate-dir` flag
2. `SSTATE_DIR` environment variable
3. `sstate_dir` key under `[build]` in `~/.config/bakar/config.toml`

If sstate is requested but none of these resolve, the sstate step is reported
as an error; the command exits non-zero only when neither cache can be acted on.

## ccache directory resolution

1. `--ccache-dir` flag
2. `[build] ccache_dir` (explicit shared path), then `[build] ccache_shared`
   (defaults to `~/.cache/bakar/ccache`) in `~/.config/bakar/config.toml`
3. the current workspace's per-workspace cache (`<workspace>/ccache`)

When none resolves (not inside a workspace and no shared cache configured), the
ccache step is skipped with a note. See
[configuration.md](configuration.md) for `ccache_shared` / `ccache_dir`.

## atime vs mtime (sstate)

`--older-than` compares each sstate file's age against the threshold. The time
basis depends on the mount option of the filesystem holding SSTATE_DIR, read
from `/proc/mounts`:

- **strictatime** mounts record a true last-read time on every access, so the
  threshold measures **last read**: a file created 60 days ago but reused in a
  build yesterday is kept.
- **relatime** (the default on most Linux systems) and **noatime** mounts do not
  give a dependable last-read time, so `clean-cache` falls back to **mtime
  (creation date)** with a warning. `relatime` updates atime at most once per
  24h and any full-tree read - a backup, `du`, or a file indexer - resets every
  file's atime at once, which silently defeats last-read eviction; `noatime`
  never updates atime at all.

```text
Warning: this filesystem is mounted relatime or noatime, so access times are not
a reliable last-read signal (a backup or indexer pass resets them). Falling back
to mtime (creation date).
Files created more than N days ago will be removed even if reused recently.
```

sstate archives are written once and never rewritten, so mtime is the creation
date and a stable age signal regardless of scans or backups. To get true
last-read semantics instead, mount the partition holding SSTATE_DIR with
`strictatime`. (ccache eviction is mtime-based and unaffected.)

## Examples

```bash
# Prune both caches, prompt first (default)
bakar clean-cache

# 60-day threshold for both, no prompt
bakar clean-cache --older-than 60 --yes

# sstate only, scan without deleting
bakar clean-cache --no-ccache --dry-run

# ccache only, evict entries older than 14 days
bakar clean-cache --no-sstate --older-than 14 --yes

# Target non-standard cache locations
bakar clean-cache --sstate-dir /mnt/shared/sstate --ccache-dir /mnt/shared/ccache --older-than 90
```

## Notes

- Empty sstate directories left behind after file removal are deleted
  automatically.
- sstate pruning is safe under concurrent builds: files are staged atomically
  before deletion, so a build reading an sstate entry never sees it disappear
  mid-read. See "sstate vs ccache pruning" above for the recovery path if the
  command is interrupted.
- sstate files that cannot be moved into the staging directory (permissions,
  race) are silently skipped; the command does not abort on partial failures.
- ccache pruning needs the `ccache` binary on PATH; it is skipped with a note
  otherwise.

## See also

- [clean.md](clean.md) - wipe the `build/` directory for a from-scratch build
- [configuration.md](configuration.md) - `SSTATE_DIR`, `ccache_shared`, `ccache_dir`, and other cache settings

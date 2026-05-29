# bakar clean-sstate

Prune stale sstate-cache entries by age to reclaim disk space.

## Synopsis

```text
bakar clean-sstate [OPTIONS]
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--older-than` | `30` | Remove files not accessed in more than N days |
| `--sstate-dir` | - | Override the SSTATE_DIR path |
| `--yes`, `-y` | - | Skip the confirmation prompt (for scripting) |
| `--dry-run`, `-n` | - | Scan and report without prompting or deleting |

## Behavior

Running the command without flags scans SSTATE_DIR, reports the number of
files and total size that match the age threshold, then prompts for
confirmation before deleting:

```text
SSTATE_DIR : /home/user/yocto-cache/sstate
Time basis : atime (last read)
Threshold  : 30 days

Found 1,247 files totalling 14.3 GiB

Delete 1,247 files (14.3 GiB)? [y/N]:
```

## SSTATE_DIR resolution

The directory is resolved in this order:

1. `--sstate-dir` flag
2. `SSTATE_DIR` environment variable
3. `sstate_dir` key under `[build]` in `~/.config/bakar/config.toml`

The command exits with an error if none of these is set.

## atime vs mtime (noatime filesystems)

When the filesystem tracks access times (`relatime` or `strictatime`),
`--older-than` measures **last read**: a file created 60 days ago but
reused in a build yesterday is kept.

On `noatime` filesystems, access times are never updated so last-read
detection is impossible. The command detects this at runtime by reading
`/proc/mounts` and falls back to **mtime (creation date)** with a warning:

```text
Warning: noatime detected on this filesystem - access times are not tracked.
Falling back to mtime (creation date).
Files created more than N days ago will be removed even if reused recently.
```

To get accurate last-read semantics, remount the filesystem with `relatime`
(the default on most modern Linux systems):

```bash
# /etc/fstab - replace noatime with relatime on the partition holding SSTATE_DIR
# Then remount without rebooting:
sudo mount -o remount,relatime /
```

`relatime` has negligible performance overhead on NVMe: it updates atime at
most once per 24 hours per file, generating a single metadata write. This is
a non-issue for sstate, where each file is read rarely and only on exact hash
matches.

## Examples

```bash
# Scan and prompt (default)
bakar clean-sstate

# Scan with a 60-day threshold and prompt
bakar clean-sstate --older-than 60

# Scan only, no prompt, no deletion
bakar clean-sstate --dry-run

# Delete without prompting (for cron jobs or automation)
bakar clean-sstate --yes

# Combine: 60-day threshold, no prompt
bakar clean-sstate --older-than 60 --yes

# Target a non-standard sstate directory
bakar clean-sstate --sstate-dir /mnt/shared/sstate --older-than 90
```

## Notes

- Empty directories left behind after file removal are deleted automatically.
- Files that cannot be stat'd or unlinked (permissions, race) are silently
  skipped; the command does not abort on partial failures.
- The scan walks the entire SSTATE_DIR tree. On a large cache (100K+ files)
  this takes a few seconds before the prompt appears.

## See also

- [clean.md](clean.md) - wipe the `build/` directory for a from-scratch build
- [configuration.md](configuration.md) - `SSTATE_DIR`, `SSTATE_MIRRORS`, and other cache env vars

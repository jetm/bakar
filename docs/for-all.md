# bakar for-all

Run a shell command once in every discovered source repo. Parity with `kas for-all-repos`.

Visits every repo even when one invocation fails. Exits non-zero if any invocation exited non-zero.

## Synopsis

```text
bakar for-all COMMAND [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `COMMAND` | | Shell command to run in each repo (required) |
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |

## Environment variables set per invocation

| Variable | Content |
|----------|---------|
| `BAKAR_REPO_NAME` | Layer name (e.g. `meta-imx`) |
| `BAKAR_REPO_PATH` | Absolute path to the repo |
| `BAKAR_REPO_COMMIT` | Full HEAD commit hash (empty string on failure) |

## Examples

```bash
# Show git status in every layer
bakar for-all "git status --short"

# Show current branch in every layer
bakar for-all "git rev-parse --abbrev-ref HEAD"

# Check for uncommitted changes
bakar for-all "git diff --stat HEAD"

# Run in every repo of a specific manifest
bakar for-all "git log --oneline -3" -f imx-6.12.49-2.2.0.xml

# Run in every repo of a BYO build
bakar for-all "git log --oneline -3" my-project.yml

# Use env vars in the command
bakar for-all 'echo "$BAKAR_REPO_NAME at $BAKAR_REPO_COMMIT"'

# Check out a specific commit in every repo (careful: overwrites working trees)
bakar for-all "git fetch origin && git checkout origin/dunfell"

# Run a multi-step command with &&
bakar for-all "git fetch origin && git log --oneline HEAD...origin/HEAD"
```

## Notes

- `COMMAND` is executed via `shell=True`; pipes, globs, and `&&` work.
- All repos are visited even when an earlier one fails. The exit code is 1 if any failed, 0 only when all succeeded.
- Source repos must be present (run `bakar build` or `bakar sync` first).

## See also

- [layers.md](layers.md) - list layer hashes without running a command
- [sync.md](sync.md) - sync sources before using for-all

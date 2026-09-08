# bakar prserv

Manage the workspace-scoped `bitbake-prserv` (PR service) daemon that keeps
package revisions monotonic across builds.

## Why this exists

meta-avocado sets `PRSERV_HOST = "localhost:0"`, bitbake's per-build autostart.
That server keeps its SQLite DB under the volatile `${PERSISTENT_DIR}`
(`TMPDIR/cache`), so wiping a build tree resets PRs to `r0` while buildhistory -
kept at `TOPDIR` - still records the old `r0.N`. The mismatch fails the
`version-going-backwards` QA on `do_packagedata_setscene` and forces cache
rebuilds. `bakar prserv` runs one persistent daemon whose DB is co-located with
the shared sstate cache, so PRs survive a `TMPDIR` wipe.

A second difference matters on a build cluster: bitbake only autostarts
`localhost:0` (`is_local_special` in `prserv/serv.py`). Binding a real,
cluster-reachable address therefore requires a managed daemon plus an explicit
`PRSERV_HOST`, which is what this command provides.

## Synopsis

```text
bakar prserv start  [KAS_YAML] [--workspace PATH]
bakar prserv stop   [KAS_YAML] [--workspace PATH]
bakar prserv status [KAS_YAML] [--workspace PATH]
```

Each verb accepts an optional positional `KAS_YAML` for BYO/bbsetup workspaces:
when given, the workspace is resolved next to that YAML (the same dispatch as
`bakar build my.yml`); omit it for an nxp/ti workspace auto-detected by walking
up from the cwd. `--workspace`/`-w` overrides the workspace root directly.

## Verbs

| Verb | Description |
|------|-------------|
| `start` | Start the daemon if its port is not already listening, then print `started: PRSERV_HOST=<host>:<port>`. Idempotent: when the port already answers, the existing address is reported without spawning anything. Exits 1 when the workspace `bitbake-prserv` binary is missing or the startup probe times out. |
| `stop` | Run `bitbake-prserv --stop` so the SQLite DB is flushed cleanly, then clear any stale pidfile. Prints `stopped`, or `not running` when the port does not answer or the binary is missing. Exits 0 either way. The PR DB is preserved. |
| `status` | Print `running, PRSERV_HOST=<host>:<port>` or `not running`. Exits 0 either way. |

## Bind address

The daemon binds `cluster_bind_host` from `~/.config/bakar/config.toml`, falling
back to `localhost` when unset. Setting it to a reachable address lets other
cluster nodes share one PR service. Liveness is a TCP probe against that
address; `0.0.0.0` and the empty string are bind-only addresses, so those are
probed on `127.0.0.1` instead.

## State files

Daemon state lives under `<state_key>/.bakar/`, where the **state key** is
`BuildConfig.prserv_state_key` - the same key `bakar hashserv` uses (the
effective `SSTATE_DIR`, falling back to `<bsp_root>` when no sstate dir is set):

| File | Contents |
|------|----------|
| `prserv.sqlite3` | The PR database (preserved across `stop`/`start`) |
| `prserv.log` | Daemon log (`bitbake-prserv -l`) |
| `prserv.stderr` | Launcher stderr, captured on every spawn attempt |

The port is derived from `sha256("prserv:" + realpath(state_key))[:8] % 16383 +
49152`. The `prserv:` salt is what keeps this daemon off the hashserv port when
both are keyed to the same shared `SSTATE_DIR`. Same state key means the same
port forever.

`bitbake-prserv --start` double-forks into a daemon and writes its own pidfile
under `/tmp/PRServer_<ip>_<port>.pid`, so bakar tracks liveness by TCP-probing
the port rather than by a tracked PID. A daemon that dies without cleaning up
leaves that pidfile behind, and `bitbake-prserv --start` then refuses to start
("Daemon already running?"). bakar removes any `/tmp/PRServer_*_<port>.pid`
before spawning and after stopping - the port is unique per state key, so the
glob cannot match an unrelated daemon.

## Binary resolution

The `bitbake-prserv` executable is searched, in order, at:

1. `<bsp_root>/sources/poky/bitbake/bin/bitbake-prserv`
2. `<bsp_root>/sources/bitbake/bin/bitbake-prserv`
3. `<bsp_root>/bitbake/bin/bitbake-prserv`
4. `<bsp_root>/../bitbake/bin/bitbake-prserv`

There is no fallback to a system `bitbake-prserv` on `PATH`: the daemon must
speak the same wire protocol as the bitbake the build runs, and only the synced
workspace guarantees that. When none of the four paths exists (sources not yet
synced), `start` fails with exit 1.

## Lifecycle

### Auto-start with `bakar build`

`bakar build` decides `PRSERV_HOST` in this order:

1. When `prserv_host` is set in config, that endpoint is exported as-is and no
   workspace daemon is started. It points the build at the central cross-node PR
   service (an avocado-linux Rust/PostgreSQL reimplementation of bitbake's PR
   service, default port 8585) which serves every node from one monotonic DB.
   This applies in both host and container mode.
2. Otherwise, a **host-mode** build starts the workspace daemon itself and
   exports the resulting `host:port`. Dry-run and script-generation paths are
   excluded, so neither spawns a daemon.
3. Otherwise nothing is exported and bitbake's own `PRSERV_HOST` value applies.

### Explicit stop

`bakar prserv stop` stops the daemon for the current state key.
`bakar clean-cache --full` also stops it before emptying the shared sstate
directory, so no live daemon runs against unlinked files.

## When the daemon will not start

`start` exits 1 (and the build path falls through to bitbake's own autostart)
when:

- The workspace `bitbake-prserv` binary is missing at all four search paths.
- The spawned daemon never opened its socket within the 5 s startup TCP probe.

In both cases the launcher's stderr is in `<state_key>/.bakar/prserv.stderr`,
which the failure message names.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `start` succeeded, or `stop`/`status` ran (regardless of running state) |
| 1 | `start` failed (binary missing or startup probe timed out) |

## Examples

```bash
# Start the daemon for the auto-detected workspace
bakar prserv start
# -> started: PRSERV_HOST=localhost:52310

# Same, for a BYO workspace selected by its kas YAML
bakar prserv start meta-avocado/kas/machine/qemuarm64.yml

# Check without changing anything
bakar prserv status
# -> running, PRSERV_HOST=localhost:52310

# Stop it (the PR DB is kept)
bakar prserv stop
```

## Gaps

`bakar prserv` has no `--json` output mode and no dedicated `bakar doctor`
check of its own; `hashserv` has both. The central-tier endpoint is configured
through `prserv_host` rather than through this command - `bakar prserv` always
manages the workspace bitbake daemon.

## See also

- [hashserv.md](hashserv.md) - the sibling daemon this one mirrors
- [build.md](build.md) - host-mode auto-start and `PRSERV_HOST` export
- [clean-cache.md](clean-cache.md) - `--full` stops the daemon before emptying sstate
- [configuration.md](configuration.md) - `cluster_bind_host` and `prserv_host` keys

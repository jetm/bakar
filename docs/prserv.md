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
| `start` | Start the daemon if the state key has none, then print `started: PRSERV_HOST=<host>:<port>`. Idempotent, with an ownership check: the address is returned only when the process on the derived port is this state key's own `bitbake-prserv`, identified by the database path in its command line, with the `--host` and `--port` in that command line matching the derived address. A listener with no daemon of the state key behind it is refused as foreign. A daemon of the state key bound to another host or port is refused with its process id and `run 'bakar prserv stop'`, never a second daemon and never its pidfile removed. Nothing is spawned in either case. A derived port the daemon cannot bind is refused before spawning, with the reason in `prserv.stderr`. Exits 1 when the workspace `bitbake-prserv` binary is missing, the derived port is busy or held by another process, a live daemon of the state key does not answer, or the startup probe times out. |
| `stop` | When bitbake's pidfile for the port exists, is owned by the current user and names the state key's daemon, run `bitbake-prserv --stop` so the SQLite DB is flushed cleanly; otherwise send SIGTERM to the state key's daemon. Every daemon of the state key is signalled, whatever host or port it is bound to, and bitbake's pidfile is read at exactly `/tmp/PRServer_<ip>_<port>.pid`. A daemon still found after bitbake's stop is sent SIGTERM too. SIGKILL is never sent. Prints `stopped`, or `not running` when no daemon of this state key is found. Exits 0 either way. The PR DB is preserved. |
| `status` | Print `running, PRSERV_HOST=<host>:<port>` or `not running`. A listener that is not the state key's daemon prints `not running`. Exits 0 either way. |

The answers of `start`, `stop` and `status` (`started: ...`, `stopped`, `not running`, `running, ...`) go to standard output. Failure messages, such as a missing binary or a startup probe timeout, go to standard error.

## Bind address

The daemon binds `cluster_bind_host` from `~/.config/bakar/config.toml`, falling
back to `localhost` when unset. Setting it to a reachable address lets other
cluster nodes share one PR service. Liveness is a TCP probe against that
address; `0.0.0.0` and the empty string are bind-only addresses, so those are
probed on `127.0.0.1` instead. A listening port alone is not proof of liveness:
the process behind it must also be the state key's own daemon serving that host and port.

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
under `/tmp/PRServer_<ip>_<port>.pid`, so bakar does not rely on that pidfile
for liveness. It finds the state key's daemon in the process table by the
`-f <state_dir>/prserv.sqlite3` argument in its command line, and probes the
port. A daemon that dies without cleaning up
leaves that pidfile behind, and `bitbake-prserv --start` then refuses to start
("Daemon already running?"). bakar removes any `/tmp/PRServer_*_<port>.pid`
before spawning and after stopping - the port is unique per state key, so the
glob cannot match an unrelated daemon. It never removes the pidfile of a daemon
that is alive.

A daemon whose pidfile was removed (for example by a temporary-directory
cleaner) is still found, returned by `start` and stopped by `stop`, because the
process table, not the pidfile, identifies it. On a host that hides the user's
own process command lines, every `start` refuses the key's own daemon as
foreign, since its ownership cannot be confirmed.

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

## Network state directories: refuse, no fallback

`ensure_running` checks the daemon's state directory (`<state_key>/.bakar`,
the effective `SSTATE_DIR`, or `<bsp_root>` when no sstate dir is configured)
*before* creating anything - state dir, SQLite DB, or process. When that state
directory resolves to NFS, or to a filesystem bakar cannot determine (an
unresolvable mount, or a path bakar cannot stat), `ensure_running` raises
`bakar.hashserv.NetworkStateDirError` instead of returning `None`.

This is deliberate and is **not** the same as the silent-fallback case in the
next section. A missing binary or a startup-probe timeout lets the build fall
through to bitbake's own per-build PR server autostart (`PRSERV_HOST =
"localhost:0"`). This one does not: there is **no fallback to a per-build PR
server** here, because that autostarted server keeps its SQLite DB under the
volatile `${PERSISTENT_DIR}` (`TMPDIR/cache`) inside the same network-backed
build tree this check exists to keep state off of, and a `TMPDIR` wipe then
resets PRs to `r0` while buildhistory still records the old `r0.N` - the
`version-going-backwards` QA failure this daemon exists to prevent in the
first place. Before this refusal existed, that silent fallback is exactly what
happened on 2026-09-25: a host-mode build with no central `prserv_host`
configured fell through to bitbake's autostart against network-backed state,
with nothing surfacing the misconfiguration.

`bakar prserv start` catches `NetworkStateDirError`, prints the refusal
message in red, and exits 1. No state dir, PID/port state, or daemon process
is left behind. The message names the resolved state directory, the
filesystem it landed on (`nfs`, or "an undetermined filesystem"), and the fix:

- Point `[build] prserv_host` at the central PR service
  (`bakar settings set build.prserv_host <host:port>`) - the per-workspace
  daemon is never started when a central endpoint is configured, so its state
  directory is never touched.
- Or set `[build] sstate_dir` to a path on local disk.

`bakar build` hits the same check through `bakar.hashserv.daemon_state_refusal(cfg)`
before launching kas-container, and fails the build with the same message
rather than silently falling through to bitbake's autostart.

## When the daemon will not start

`start` exits 1 (and the build path falls through to bitbake's own autostart)
when:

- The workspace `bitbake-prserv` binary is missing at all four search paths.
- The derived port is busy: a bind test refuses a port the daemon cannot bind
  before bitbake's pidfiles are touched and before anything is spawned.
- The derived port is held by a foreign listener, a process that is not this
  state key's `bitbake-prserv`. The message reads `<host>:<port> is held by a
  process that is not this state key's bitbake-prserv; bitbake-prserv was not
  started`.
- A daemon of the state key serves another host or port (for example after
  `cluster_bind_host` changed): `bitbake-prserv pid <pid> already serves <db> on
  <host>:<port>, not <host>:<port>; run 'bakar prserv stop'`. Nothing is spawned
  and its pidfile is kept.
- The state key's daemon exists but does not answer on the port within the 5 s
  probe: `bitbake-prserv pid <pid> already serves <db> but does not answer on
  <host>:<port>; run 'bakar prserv stop'`.
- The spawned daemon never opened its socket within the 5 s startup TCP probe.

Every refusal and spawn failure writes its reason to
`<state_key>/.bakar/prserv.stderr`, which the failure message names. A missing
binary is the exception: it writes nothing.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `start` succeeded, or `stop`/`status` ran (regardless of running state) |
| 1 | `start` failed (binary missing, derived port busy or held by a foreign listener, a live daemon that does not answer, or startup probe timed out) |

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

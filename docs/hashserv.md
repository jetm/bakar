# bakar hashserv

Manage the workspace-scoped `bitbake-hashserv` daemon that backs OEEquivHash sstate equivalence across builds.

## Why this exists

`BB_HASHSERVE = "auto"` (the default in `overlays/bakar-tuning-hashequiv.yml`) makes bitbake start a fresh hash-equivalence server per build, populate it from scratch, and tear it down at build end. Cross-build equivalence — the whole point of OEEquivHash — never accumulates. `bakar hashserv` runs one persistent daemon per workspace so the cache survives.

## Synopsis

```text
bakar hashserv start
bakar hashserv stop
bakar hashserv status
```

## Verbs

| Verb | Description |
|------|-------------|
| `start` | Spawn the daemon if it is not already running. Idempotent: a second `start` does nothing when the PID is alive. Exits 1 when the workspace's `bitbake-hashserv` binary is missing or the startup TCP probe fails. |
| `stop` | Signal SIGTERM, wait up to 5 s for a graceful flush, then SIGKILL if still alive. Removes the PID and port state files. The SQLite hash database is preserved so the next `start` resumes the cache. |
| `status` | Print `running, pid=<N>, url=ws://localhost:<port>` or `not running`. Exits 0 either way. |

## State files

Daemon state lives under `<bsp_root>/.bakar/`:

| File | Contents |
|------|----------|
| `hashserv.pid` | PID of the live daemon process (removed on `stop`) |
| `hashserv.port` | TCP port the daemon is bound to (removed on `stop`) |
| `hashserv.db` | SQLite hash equivalence store (preserved across `stop`/`start`) |
| `hashserv.stderr` | Daemon stderr captured when startup fails (rewritten on each failed `ensure_running`) |

The port is derived from `sha256(realpath(bsp_root))[:8] % 16383 + 49152` so two workspaces on the same machine never collide on the loopback listener. Same workspace path → same port forever.

## Lifecycle

### Auto-start with `bakar build`

When `[build] hashserv = true` is set in `~/.config/bakar/config.toml`, every `bakar build` calls `hashserv.ensure_running(bsp_root)` before launching kas-container:

1. If a live daemon already exists for this workspace, it is reused.
2. Otherwise, `<bsp_root>/sources/poky/bitbake/bin/bitbake-hashserv` is spawned with `--bind ws://localhost:<port>` and `--database <bsp_root>/.bakar/hashserv.db`, detached from the bakar process group via `start_new_session=True`.
3. A 2 s TCP probe gates the return: until `socket.create_connection` succeeds, the daemon is not considered ready. If the probe times out the spawned process is SIGTERMed, its stderr is captured to `hashserv.stderr`, and the build falls through to `BB_HASHSERVE=auto` (the legacy transient behaviour).

The daemon persists across `bakar` exits. A subsequent build, doctor invocation, or `hashserv status` finds it alive and reuses it. Implicit shutdown does **not** happen on bakar exit — that is the whole point: the cache must survive between runs.

### Explicit stop

`bakar hashserv stop` and `bakar clean --all` both stop the daemon. `clean --all` calls `hashserv.stop(bsp_root)` *before* removing workspace directories so the daemon does not end up orphaned with a missing working directory.

## Container build wiring

In container mode (the default — no `--host` flag), the in-container bitbake cannot reach `ws://localhost:<port>` on the host because Docker's bridge network isolates the container's loopback. Two pieces of plumbing make it work:

1. **Env rewrite.** `_build_env` rewrites the URL from `ws://localhost:<port>` to `ws://host.docker.internal:<port>` before it lands in the container env.
2. **`--add-host`.** The kas-container invocation gets `--runtime-args "-v <ccache>:/work/ccache:rw --add-host=host.docker.internal:host-gateway"` so Docker plants a `host.docker.internal` entry in the container's `/etc/hosts` that resolves to the host bridge gateway.

`--add-host=...:host-gateway` requires Docker 20.10 or newer. `bakar doctor` warns when the daemon is older.

In host mode (`bakar build --host`), the rewrite is skipped — `BB_HASHSERVE` stays at `ws://localhost:<port>` because bitbake runs directly on the host and reaches the daemon on the loopback interface without translation.

## Overlay loading — automatic when `[build] hashserv = true`

When `cfg.use_hashequiv` is True, `bakar build` auto-appends the `bakar-tuning-hashequiv.yml` overlay to the build's overlay list, so a single config flip is enough to wire the full chain:

1. `[build] hashserv = true` in `~/.config/bakar/config.toml`.
2. (Nothing else.) `bakar build` now both starts the persistent daemon AND loads the overlay that switches `BB_SIGNATURE_HANDLER` to `OEEquivHash`.

Mechanically: `commands/build.py` calls `_hashequiv_extra_overlays(cfg)` (defined in `commands/_helpers.py`) at both dispatch sites (manifest-driven and bbsetup paths). The helper returns the hashequiv overlay path when the config is true and the file exists, else `[]`. The result is appended to whatever `extra_overlays` the user passed via the colon-joined kas YAML argument.

Users who pass the overlay explicitly (e.g. `bakar build my.yml:bakar-tuning-hashequiv.yml`) are deduplicated via `Path.resolve()` comparison — the overlay is loaded exactly once even when both sources name it.

Without `hashserv = true`, no auto-append happens and `BB_SIGNATURE_HANDLER` stays at the default `OEBasicHash`. The daemon would not be started either, so this is a consistent off-by-default behavior.

## doctor integration

`bakar doctor` runs `check_hashserv(cfg)`:

| Condition | Result |
|-----------|--------|
| `[build] hashserv = false` (or unset) | SKIP (INFO) |
| Daemon configured, PID alive, TCP probe succeeds | PASS (WARN) |
| Daemon configured, PID dead or PID file absent | FAIL (WARN) → `bakar hashserv start` |
| Daemon configured, PID alive, TCP probe fails | FAIL (WARN) → `bakar hashserv stop && bakar hashserv start` |

The TCP probe (1 s timeout, against the derived port) catches a wedged daemon that the PID-check alone would falsely pass. The check tolerates concurrent state-file deletion: if `hashserv stop` removes the PID/port files between the PID check and the file read, the check degrades to FAIL rather than propagating `FileNotFoundError`.

The check is in `SHARED_CHECKS` and excluded from `_DOCKER_CHECKS` — the daemon runs on the host in both container and host build mode, so the check runs in both.

## Configuration

```toml
[build]
hashserv = true   # default: false
```

Set via `bakar settings set build.hashserv true` or by hand-editing `~/.config/bakar/config.toml`. See [settings.md](settings.md) and [configuration.md](configuration.md).

## Examples

```bash
# Opt in once
bakar settings set build.hashserv true

# First build — daemon spawns and the hashequiv overlay auto-loads
bakar build -f imx-6.12.49-2.2.0.xml

# Inspect the running daemon
bakar hashserv status
# -> running, pid=482917, url=ws://localhost:51847

# Subsequent builds reuse the same daemon — cache content accumulates
bakar build -f imx-6.12.49-2.2.0.xml

# Stop the daemon explicitly (e.g. before swapping the workspace's bitbake version)
bakar hashserv stop

# Stop and wipe everything including the cache
bakar clean --all
```

## Protocol compatibility

The daemon launched at `<bsp_root>/sources/poky/bitbake/bin/bitbake-hashserv` is the same bitbake the build will use, so client and server speak the same protocol version always. No PATH fallback to a system `bitbake-hashserv` exists — a system binary may speak a different protocol and would silently mismatch.

This implies one workspace pins one bitbake version. If you point the same `<bsp_root>` at a different upstream (e.g. kirkstone → scarthgap), stop the daemon first; the next build will spawn the new bitbake's hashserv against the same SQLite database, which the daemon migrates automatically.

## When the daemon will not start

`ensure_running` returns `None` (silently — no error) and the build falls through to `BB_HASHSERVE=auto` in these cases:

- The workspace bitbake-hashserv binary is missing (sources not yet synced).
- The SQLite database under `<bsp_root>/.bakar/hashserv.db` cannot be written (read-only filesystem, exhausted quota).
- The derived port is already bound by another process. (Hash collision probability ≈ 1-in-16383 per workspace pair; a real conflict surfaces as a SKIP in doctor with the daemon stderr in `hashserv.stderr`.)
- The 2 s startup TCP probe times out (the daemon spawned but never opened its socket).

In every failure case the daemon stderr is captured to `<bsp_root>/.bakar/hashserv.stderr` for diagnosis.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `start` succeeded, `stop` ran, or `status` reported (regardless of running state) |
| 1 | `start` failed (binary missing or startup probe failed) |

## See also

- [build.md](build.md) — daemon auto-start happens here when configured
- [clean.md](clean.md) — `--all` stops the daemon before the wipe
- [doctor.md](doctor.md) — `check_hashserv` PID + TCP probe details
- [configuration.md](configuration.md) — `[build] hashserv` config key and resolution order
- [settings.md](settings.md) — `bakar settings set build.hashserv true` CRUD path

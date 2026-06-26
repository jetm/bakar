# bakar sccache-dist

Route bitbake's C/C++ `do_compile` work through an [sccache-dist](https://github.com/mozilla/sccache/blob/main/docs/DistributedQuickstart.md) scheduler so a cold Yocto build can pool cores across machines on the LAN. bakar configures only the **client**: it appends the sccache overlay, exports the scheduler URL into the build, and verifies reachability in `bakar doctor`. The **scheduler** and **build-server** are long-lived host services you manage with systemd.

This runbook covers the operator setup and **Phase 1**: single-PC validation, where the scheduler, build-server, and sccache client all run on one machine. Phase 1 derisks the wiring (does `CCACHE = "sccache "` actually route `CC` through sccache? does Linux auto-packaging ship Yocto's per-recipe cross-toolchain?) before adding a second machine. The same client knob — `--sccache-scheduler URL` — is identical for a 1-node, 2-node, or N-node office cluster; growth is operator-side (register another build-server with the scheduler).

## Architecture

| Component | Port | Runs as | Managed by | Needs |
|-----------|------|---------|------------|-------|
| Scheduler | 10600 | unprivileged user | systemd (operator) | reachable on the LAN |
| Build-server | 10501 | **root** | systemd (operator) | bubblewrap (`bwrap`) |
| Client | — | your user | bakar (overlay + env) | `sccache` on PATH, `~/.config/sccache/config` |

The scheduler hands compile jobs from clients to build-servers. The build-server runs each compile inside a bubblewrap sandbox, so it needs root and the `bwrap` binary. On Linux, sccache auto-packages the in-use compiler and ships it to the build-server, which is what lets Yocto's per-recipe cross-toolchains compile remotely without static per-toolchain config.

## What bakar wires, and what it does not

bakar owns the client side only:

- When `cfg.use_sccache_dist` is true, `bakar build` appends `overlays/bakar-tuning-sccache.yml`, which sets `CCACHE = "sccache "`, `INHERIT:remove = "ccache"` (ccache and sccache are mutually-exclusive launchers — chaining them double-wraps `CC`), and exports `SCCACHE_DIST_SCHEDULER_URL` from the env bakar passes in.
- `_build_env` exports `BAKAR_SCCACHE_SCHEDULER_URL = cfg.sccache_scheduler_url` into the build, but only when distributed compile is enabled. A disabled build is byte-for-byte unchanged: ccache stays as today.
- `bakar doctor` runs the `sccache-dist` check (see [doctor integration](#doctor-integration)).

bakar does **not** start, supervise, or configure the scheduler or build-server — those are root services and stay out of bakar entirely. The overlay's exported env var carries the scheduler URL, but the sccache client still reads its auth token and full dist config from `~/.config/sccache/config`. You set that file up once, below.

## Prerequisites

- `sccache` on PATH. Check with `command -v sccache`.
- `sccache-dist` on PATH for the scheduler and build-server. **This binary only exists in a source build with the dist server feature** - upstream's release tarballs ship the client alone, so any prebuilt `-bin` package (e.g. Arch's `sccache-bin`) gives you `sccache` but not `sccache-dist`. Confirm with `command -v sccache-dist`. Install a source-built package (Arch's `extra/sccache` builds it) or build it yourself: `cargo install sccache --features=dist-client,dist-server`.
- bubblewrap for the build-server. On Arch: `sudo pacman -S bubblewrap`, then confirm `ls /usr/bin/bwrap`.
- Phase 1 runs in **host mode** (kas runs directly on the host). The wrynose example uses a kas-container image; host mode is auto-selected when no container image is set, so pass `--host` or run an example with no `image:` key. The container path is validated separately in Phase 2.

## Operator setup (systemd)

### 1. Generate JWT secrets

The scheduler and build-server authenticate with a shared HS256 secret; clients authenticate to the scheduler with the same secret, and each build-server gets a server token derived from it.

```bash
# Scheduler/client shared secret
sccache-dist auth generate-jwt-hs256-key
# -> SECRET_KEY (save it)

# Build-server token, bound to the server's listen address
sccache-dist auth generate-jwt-hs256-server-token \
  --secret-key <SECRET_KEY> --server 127.0.0.1:10501
# -> SERVER_JWT (save it)
```

For Phase 1 the build-server listens on loopback (`127.0.0.1:10501`). For Phase 2 regenerate the server token against the build-server's LAN address.

### 2. Scheduler config — `/etc/sccache/scheduler.toml`

```toml
public_addr = "127.0.0.1:10600"

[client_auth]
type = "token"
token = "<SECRET_KEY>"

[server_auth]
type = "jwt_hs256"
secret_key = "<SECRET_KEY>"
```

### 3. Build-server config — `/etc/sccache/server.toml`

```toml
cache_dir = "/tmp/sccache-toolchains"
public_addr = "127.0.0.1:10501"
scheduler_url = "http://127.0.0.1:10600"

[builder]
type = "overlay"
build_dir = "/tmp/sccache-build"
bwrap_path = "/usr/bin/bwrap"

[scheduler_auth]
type = "jwt_token"
token = "<SERVER_JWT>"
```

The `overlay` builder runs each compile inside bubblewrap; `bwrap_path` must point at the installed `bwrap`. Confirm `ls /usr/bin/bwrap` before starting the unit.

### 4. Client config — `~/.config/sccache/config`

```toml
[dist]
scheduler_url = "http://localhost:10600"

[dist.auth]
type = "token"
token = "<SECRET_KEY>"
```

This is the file bakar's overlay does **not** write. The overlay exports `SCCACHE_DIST_SCHEDULER_URL` so the in-build client knows the scheduler, but the auth token lives here.

### 5. systemd units

`/etc/systemd/system/sccache-scheduler.service`:

```ini
[Unit]
Description=sccache-dist scheduler
After=network.target

[Service]
ExecStart=/usr/bin/sccache-dist scheduler --config /etc/sccache/scheduler.toml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/sccache-server.service`:

```ini
[Unit]
Description=sccache-dist build-server
After=network.target sccache-scheduler.service

[Service]
# Build-server requires root for the bubblewrap overlay builder.
User=root
ExecStart=/usr/bin/sccache-dist server --config /etc/sccache/server.toml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sccache-scheduler.service
sudo systemctl enable --now sccache-server.service
sudo systemctl status sccache-scheduler.service sccache-server.service
```

### 6. Start the client and confirm it sees the scheduler

```bash
sccache --stop-server
SCCACHE_CONF=~/.config/sccache/config sccache --start-server
sccache --dist-status
```

`--dist-status` must report a `SchedulerStatus` with at least one server, not `"Disabled"`. `"Disabled"` means the client never loaded the dist config — check `SCCACHE_CONF` points at the file from step 4 and that `[dist].scheduler_url` is set.

### 7. Trivial compile proof

Before invoking a full Yocto build, prove the round trip with one C file:

```bash
echo 'int main(void){return 0;}' > /tmp/t.c
SCCACHE_CONF=~/.config/sccache/config sccache cc -c /tmp/t.c -o /tmp/t.o
sccache --show-stats
```

`--show-stats` should show a non-zero compile-request count and, if distribution is working, a non-zero "Successful distributed compiles" line. If everything is local-only here, distribution will not work for the Yocto build either — fix the scheduler/server before continuing.

## Phase 1 — single-PC validation

With the scheduler, build-server, and client all up on this one PC, run the wrynose build in host mode through bakar:

```bash
cd ~/repos/personal/bakar
bakar build examples/kas-qemux86-64-wrynose.yml \
  --host \
  --sccache-dist \
  --sccache-scheduler http://localhost:10600
```

`bakar doctor` runs first and gates the build (see [doctor integration](#doctor-integration)); if the `sccache-dist` check is BLOCK, the build stops with an actionable message before any compile runs.

### Validation checks

Run these and record the results. Each maps to a design assumption (A1, A2) the runbook exists to falsify.

**1. The `sccache` launcher reaches the compiler (A1).**

Check the launcher variable directly. `CCACHE` is global, so it resolves without a recipe scope:

```bash
bakar getvar CCACHE        # -> "sccache " when the overlay is live
```

`CC` itself is per-recipe - global `CC` is undefined in this OE, so `bakar getvar CC` alone reports "CC is not defined". Confirm the prefix lands on a real compiler command with a recipe scope:

```bash
bakar getvar CC --recipe quilt-native   # -> export CC="sccache gcc "
```

An empty `CCACHE`, or a `CC` with no `sccache` prefix, means the overlay's `CCACHE = "sccache "` did not reach the parse - the proposal's first falsifier. Check the overlay merged (`bakar dump` shows `CCACHE = "sccache "`) and that no recipe overrides `CCACHE`.

**2. sccache logged compile requests (A1).**

```bash
sccache --show-stats
```

The compile-request count must be `> 0` after the build. Zero requests means nothing routed through sccache despite the prefix — re-check `SCCACHE_CONF` and that the build env carried `BAKAR_SCCACHE_SCHEDULER_URL`.

**3. The image built correctly.**

The wrynose build must produce a valid `core-image-minimal`. A distributed-compile change that breaks the image is a regression, not a win.

**4. buildstats compile-fraction breakdown.**

This is the Amdahl check: if `do_compile` is a small fraction of total build CPU-time, distribution cannot help much regardless of how well it works.

```bash
# buildstats land under the build's tmp dir, one dir per build timestamp.
# Sum do_compile CPU-time vs. total across all recipes:
BS=$(find . -path '*/buildstats/*' -name 'build_stats' | sort | tail -1 | xargs dirname)
grep -rh '^Elapsed time' "$BS"/*/do_compile 2>/dev/null | \
  awk '{s+=$4} END{print "do_compile elapsed (s):", s}'
```

Record `do_compile` CPU-time as a fraction of total build CPU-time. If compile is a minority of total, note in the result that distribution may not be worth pursuing to Phase 2 — that is a legitimate falsification of the whole approach, not a failure of this task.

### When a check fails

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `bakar getvar CCACHE` is empty (or `getvar CC --recipe` shows no prefix) | Overlay not loaded, or recipe overrides `CCACHE` | Confirm `--sccache-dist` is set; check `bakar dump` shows the overlay; check for per-recipe `CCACHE` overrides |
| `sccache --show-stats` shows 0 compile requests | Client config not read inside the build | Verify `SCCACHE_CONF` and that `BAKAR_SCCACHE_SCHEDULER_URL` reached the build env |
| `sccache --dist-status` reports `Disabled` | Client never loaded dist config | Fix `~/.config/sccache/config` `[dist].scheduler_url`; restart the client |
| Build fails inside a remote compile | Auto-packaging could not ship the cross-toolchain (A2) | Phase 1 is loopback-only, so this is rare; for Phase 2, declare per-arch toolchains in the client config or run local-cache-only |

## doctor integration

`bakar doctor` runs the `sccache-dist` check, which gates `bakar build`:

| Condition | Result |
|-----------|--------|
| `[build] sccache_dist = false` (or unset) | SKIP (INFO) — nothing to verify, build unchanged |
| Enabled, `sccache` binary missing from PATH | FAIL (BLOCK) → install sccache |
| Enabled, no scheduler URL set | FAIL (BLOCK) → set `[build] sccache_scheduler_url` or pass `--sccache-scheduler` |
| Enabled, scheduler URL has no host:port | FAIL (BLOCK) → use a URL like `http://localhost:10600` |
| Enabled, scheduler host:port unreachable | FAIL (BLOCK) → start the scheduler, confirm URL/port |
| Enabled, binary present and scheduler reachable | PASS (BLOCK severity) |

The reachability probe is a 1-second TCP `create_connection` to the host:port parsed from `sccache_scheduler_url`, mirroring the hashserv probe. A missing prerequisite fails fast at BLOCK severity rather than silently degrading to a local-only compile.

## Checking cluster capacity

`bakar cluster-info` queries the scheduler and prints its live capacity, so you can confirm the cluster is up and sized as expected before kicking off a build:

```bash
bakar cluster-info
# sccache-dist cluster:
#   scheduler: http://localhost:10600
#   build servers: 2
#   cpus: 64
#   jobs in progress: 3
```

The scheduler URL resolves from `--scheduler`, then the global `--sccache-scheduler`, then `sccache_scheduler_url` in config. `--json` emits a machine-readable document (`reachable`, `scheduler_url`, `error`, `capacity`); the command exits 1 when the scheduler is unreachable or `sccache` is not installed.

The scheduler exposes only aggregate counts (server count, total CPUs, jobs in progress) — there is no per-build-server breakdown. When a build-server array becomes available from the scheduler, `cluster-info` prints it as a node list with no further change.

`cluster-info` is a one-shot snapshot. To watch the cluster *and* the build together while a build runs — per-node job load, the daemon's cache/distributed/fell-back counts, and bitbake task progress in one refreshing view (or `--json`/NDJSON for CI) — use [`bakar monitor`](monitor.md).

## Configuration

```toml
[build]
sccache_dist = true                              # default: false
sccache_scheduler_url = "http://localhost:10600" # default: unset
```

Set via `bakar settings set build.sccache_dist true` and `bakar settings set build.sccache_scheduler_url http://localhost:10600`, or hand-edit `~/.config/bakar/config.toml`. The `--sccache-dist` / `--sccache-scheduler URL` CLI flags override config per-build. See [settings.md](settings.md) and [configuration.md](configuration.md).

## See also

- [build.md](build.md) — doctor runs automatically before every build
- [doctor.md](doctor.md) — the doctor gate that runs the `sccache-dist` check
- [hashserv.md](hashserv.md) — the host-gateway / `--add-host` plumbing the container path reuses for the in-container client
- [configuration.md](configuration.md) — `[build] sccache_dist` / `sccache_scheduler_url` resolution order

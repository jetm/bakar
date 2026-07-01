#!/usr/bin/env bash
# Reset the sccache-dist cluster and Yocto caches to a cold state, for
# from-scratch distributed-build testing.
#
# Wipes local sstate + the build dir + the sccache client disk cache, stops the
# client daemon, then wipes and re-initialises the sccache-dist SERVER caches on
# every cluster node and restarts their services.
#
# Secondary nodes are auto-detected LIVE from the sccache-dist scheduler
# (`sccache --dist-status` -> .SchedulerStatus[1].servers[].id), with this
# host's own server dropped by matching each server IP against the local
# interface addresses. Nothing is hardcoded: a node added to (or removed from)
# the running cluster is picked up automatically, and if the scheduler reports
# only the local server, PC1 is reset alone. Override with SECONDARY_NODES=
# "h1 h2" (space-separated) when the scheduler is unavailable.
#
# The build server does a NON-recursive mkdir of build/toolchains/<hash> per job,
# so build/toolchains must exist or every distributed compile fails with "failed
# to prepare overlay dirs" (HTTP 500). Recreate that subdir explicitly - a bare
# build/ is not enough - and restart the server so its in-memory toolchain refs
# match the wiped disk.
#
# Override the environment-specific paths via env vars, e.g.
#   BUILD_DIR=~/ws/build-x ./clean-all-cache.sh
#
# -e is intentionally omitted: pkill exits non-zero when no daemon is running and
# the remote reset is best-effort, neither of which should abort the local wipe.
set -uo pipefail

SSTATE_DIR="${SSTATE_DIR:-$HOME/yocto-cache/sstate}"
BUILD_DIR="${BUILD_DIR:-$HOME/repos/work/peridio-scarthgap-build/build-qemuarm64}"

# Resolve the secondary (non-local) build servers to reset. Precedence: an
# explicit SECONDARY_NODES override, else the live server list reported by the
# sccache-dist scheduler with this host's own server(s) filtered out.
resolve_secondaries() {
  if [[ -n "${SECONDARY_NODES:-}" ]]; then
    # shellcheck disable=SC2086 # intentional word-split of the space-separated list
    printf '%s\n' ${SECONDARY_NODES}
    return 0
  fi
  command -v sccache >/dev/null 2>&1 || return 0
  # sccache --dist-status routes through the local client daemon, which the
  # first call auto-starts - so retry until the scheduler reports its servers.
  local dist_status="" locals
  for _ in 1 2 3; do
    dist_status="$(sccache --dist-status 2>/dev/null)"
    [[ "$dist_status" == *'"servers"'* ]] && break
    sleep 1
  done
  [[ -n "$dist_status" ]] || return 0
  locals="$(ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
  # Pass status + local IPs as argv (a heredoc on `python3 -` would otherwise
  # override any piped stdin, per shellcheck SC2259).
  python3 - "$dist_status" "$locals" <<'PY'
import sys, json
try:
    status = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
locals_ = set(sys.argv[2].split())
servers = (status.get("SchedulerStatus") or [None, {}])[1].get("servers", [])
seen = set()
for srv in servers:
    host = (srv.get("id") or "").rsplit(":", 1)[0]
    if host and host not in locals_ and host not in seen:
        seen.add(host)
        print(host)
PY
}
mapfile -t NODES < <(resolve_secondaries)

# Local Yocto + sccache client disk caches.
rm -rf "$SSTATE_DIR" && mkdir -p "$SSTATE_DIR" # dir must exist for bakar doctor
rm -rf "$BUILD_DIR"
rm -rf "$HOME/.cache/sccache" "$HOME/.cache/sccache-dist-client" "$HOME"/.cache/sccache-dist-client.stale.*

# Stop the persistent sccache client daemon so it does not keep serving the wiped
# disk cache; bakar re-starts it on its unix socket on the next build. sccache
# unlinks a stale socket on the next bind, so the .sock needs no explicit rm.
pkill -f '^/usr/bin/sccache$' 2>/dev/null || true

# sccache-dist SERVER caches: wipe, recreate the toolchains subdir the server
# expects under build/, and restart the service so it re-inits cleanly.
reset_dist_server='sudo rm -rf /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build \
  && sudo mkdir -p /var/cache/sccache-dist/toolchains /var/cache/sccache-dist/build/toolchains \
  && sudo systemctl restart sccache-server'

# PC1 (local).
bash -c "$reset_dist_server"

# Every auto-detected secondary node over the direct link.
if [[ ${#NODES[@]} -eq 0 ]]; then
  echo "clean-all-cache: no secondary build servers detected; reset PC1 only." >&2
fi
for node in "${NODES[@]}"; do
  echo "clean-all-cache: resetting sccache-dist server on ${node} ..."
  ssh -t "$node" "$reset_dist_server"
done

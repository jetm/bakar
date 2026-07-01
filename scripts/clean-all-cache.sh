#!/usr/bin/env bash
# Reset the sccache-dist cluster and Yocto caches to a cold state, for
# from-scratch distributed-build testing.
#
# Wipes local sstate + the build dir + the sccache client disk cache, stops the
# client daemon, then wipes and re-initialises the sccache-dist SERVER caches on
# both cluster nodes and restarts their services.
#
# The build server does a NON-recursive mkdir of build/toolchains/<hash> per job,
# so build/toolchains must exist or every distributed compile fails with "failed
# to prepare overlay dirs" (HTTP 500). Recreate that subdir explicitly - a bare
# build/ is not enough - and restart the server so its in-memory toolchain refs
# match the wiped disk.
#
# Override the environment-specific paths/host via env vars, e.g.
#   SECONDARY_NODE=10.42.0.3 BUILD_DIR=~/ws/build-x ./clean-all-cache.sh
#
# -e is intentionally omitted: pkill exits non-zero when no daemon is running and
# the remote reset is best-effort, neither of which should abort the local wipe.
set -uo pipefail

SSTATE_DIR="${SSTATE_DIR:-$HOME/yocto-cache/sstate}"
BUILD_DIR="${BUILD_DIR:-$HOME/repos/work/peridio-scarthgap-build/build-qemuarm64}"
SECONDARY_NODE="${SECONDARY_NODE:-10.42.0.2}"

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

# Secondary node over the direct link.
ssh -t "$SECONDARY_NODE" "$reset_dist_server"

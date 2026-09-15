# `build-listing`: A new read-only `bakar ps` command that discovers every live bakar build on the host (container-mode via a single selected runtime's label query, host-mode via a `/proc` argv scan), deduplicates within and across sources, and reports one row per live build with `--json` support

**Delivered by:** stop-multi-build-select
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar stop --run <run-id>` to target one of several live builds in a workspace; `bakar ps` (optionally `--json`) to list every live build on the host
Precondition: none beyond at least one build somewhere on the host having been launched by `bakar build` (which writes `build.meta.json` via `write_launch_record`, `build_stop.py:145-167`) - `bakar ps` itself resolves no workspace and can be run from anywhere
Success signal: `bakar stop --run <id>` exits 0 and only that run's process/container is signalled, verified by every other live run's PID/container remaining alive and its run directory unchanged; `bakar ps --json` exits 0 with a well-formed JSON array containing exactly one object per live build
Silent failure: none - an unmatched `--run`, zero live builds, or an unreachable container runtime each produce an explicit message (a stderr warning for the runtime case, since `bakar ps` degrades rather than fails there)

# `stop-workspace-selection`: Workspace-wide live-build discovery for `bakar stop`, exact-match `--run` targeting, refuse-and-list on ambiguity, and an interactive TTY pick - all built on top of the existing single-build stop mechanics, which are extracted but not behaviorally changed

**Delivered by:** stop-multi-build-select
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar stop --run <run-id>` to target one of several live builds in a workspace; `bakar ps` (optionally `--json`) to list every live build on the host
Precondition: none beyond at least one build somewhere on the host having been launched by `bakar build` (which writes `build.meta.json` via `write_launch_record`, `build_stop.py:145-167`) - `bakar ps` itself resolves no workspace and can be run from anywhere. `bakar stop` no longer requires a workspace either: when invoked with no `--workspace`, no cwd-resolvable workspace, and no BYO kas YAML, it falls back to scanning every host-mode build on the host (container-mode runs are invisible to this fallback, and the scan is filtered to the topdirs `_discover_host_cookers` finds live cookers under, not every topdir the host has ever built in) and always confirms interactively before stopping - `--force` bypasses that confirmation only when paired with an explicit `--run <id>`, never on the bare no-selector host-wide path
Success signal: `bakar stop --run <id>` exits 0 and only that run's process/container is signalled, verified by every other live run's PID/container remaining alive and its run directory unchanged; `bakar ps --json` exits 0 with a well-formed JSON array containing exactly one object per live build
Silent failure: none - an unmatched `--run`, zero live builds, or an unreachable container runtime each produce an explicit message (a stderr warning for the runtime case, since `bakar ps` degrades rather than fails there)

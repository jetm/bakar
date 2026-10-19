# `detached-build-launch`: `--detach` on build, bitbake and rebuild - unit launch, handle line, run-id registration, refusals when detaching is impossible or meaningless, bounded record retention

**Delivered by:** bakar-run-lifecycle-for-agents
**Modules touched:** hooks, other, tests

## Delivery Note

Invocation: bakar build kas/machine/qemux86-64.yml --target quilt-native --detach, then bakar wait <run_id> --timeout 540
Precondition: a systemd user manager reachable from the shell with XDG_RUNTIME_DIR set (linger enabled to survive logout); for the rule and hook, devtool project sync run for peridio, variscite and bakar
Success signal: one JSON handle line on stdout and exit 0 within 60 s; wait exits 0 (succeeded), 1 (failed), 3 (unknown) or 124 (still running at the timeout); status --json stays readable after the unit exits
Silent failure: a build started without --detach has no registry record and a run from before this change reports unknown; CLAUDECODE-gated hints never print under a harness that does not set CLAUDECODE

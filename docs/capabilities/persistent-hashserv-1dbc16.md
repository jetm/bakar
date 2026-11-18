# `persistent-hashserv`: ADDED requirement stating how the workspace daemon start behaves when its derived port cannot be bound (refuse before spawning, never report a daemon that did not bind, leave no state files, record the reason)

**Delivered by:** hashserv-port-collision
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar hashserv start` (CLI) and `hashserv.ensure_running(state_key, binary_root=bsp_root)` (build path, `src/bakar/steps/kas_build.py:1979`)
Precondition: workspace `bitbake-hashserv` synced under `<bsp_root>/sources/poky/bitbake/bin/`
Success signal: `started: ws://localhost:<port>` and exit 0 on a free port; on a held port exit 1 with the cause list in the message and the port named in `<state_key>/.bakar/hashserv.stderr`
Silent failure: the build path (`kas_build.py:1979`) still treats None as "run without the persistent hashserv" and prints nothing, so a held port costs that one build the persistent daemon

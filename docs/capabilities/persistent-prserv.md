# `persistent-prserv`: the per-workspace PR service start, status and stop contract, which today has no specification: the daemon on the derived port is the state key's own, a foreign or unbindable port is refused with a recorded reason, and an orphaned daemon is reachable by stop and status.

**Delivered by:** hashserv-orphan-adoption
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar prserv start`, `bakar prserv stop` and `bakar prserv status` in a workspace whose daemon has lost its bitbake pidfile; the build path reaches the same code through `prserv.ensure_running(state_key, binary_root=bsp_root)`
Precondition: the state directory is on local disk (a network one is refused first); the workspace bitbake-prserv binary is needed only to spawn, not to adopt a live daemon; a host that hides the user's own process command lines degrades to the previous refusal
Success signal: `started: PRSERV_HOST=<host>:<port>` with exit 0 and no second daemon process; `stopped` after a stop with no process left on the state key's database; `running, PRSERV_HOST=<host>:<port>` from status only for the state key's own daemon
Silent failure: a foreign listener on the prserv port or a live daemon that cannot be adopted makes `ensure_running` return None with nothing printed during a build, which then runs without the persistent daemon; the only record is the reason line in `prserv.stderr` under `<state_key>/.bakar`

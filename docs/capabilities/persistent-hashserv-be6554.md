# `persistent-hashserv`: adds the requirement that a live daemon of the state key with no PID file is adopted on start, reached by stop, and refused with a named process when it cannot be adopted

**Delivered by:** hashserv-orphan-adoption
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar hashserv start` and `bakar hashserv stop` in a workspace whose daemon has lost its PID file; the build path reaches the same code through `hashserv.ensure_running(state_key, binary_root=bsp_root)`
Precondition: the state directory is on local disk (a network one is refused first); `[build] hashserv = true` for the build path; the workspace bitbake-hashserv binary is needed only to spawn, not to adopt a live daemon; a host that hides the user's own process command lines degrades to the previous refusal
Success signal: `started: ws://localhost:<port>` with exit 0 and no second daemon process; `stopped` after a stop with no process left on the state key's database
Silent failure: a live daemon that cannot be adopted (never answers, deleted database, other bind address) makes `ensure_running` return None with nothing printed during a build, which then runs without the persistent daemon; the only record is the reason line in `hashserv.stderr` under `<state_key>/.bakar`

# `concern-single-ownership`: adds the requirement that identifying a daemon's own process has one implementation shared by the hashserv and prserv modules

**Delivered by:** hashserv-orphan-adoption
**Modules touched:** other, tests

## Delivery Note

Invocation: `central_service.find_owned_daemons(tool, database)` called by the hashserv and prserv modules with their tool name and state-key database path
Precondition: none
Success signal: returns a list of `OwnedDaemon(pid, argv)` for the current user's live processes whose first or second argument names the tool and which carry the database path as a whole argument; `[]` for another database or an exited daemon
Silent failure: a host that hides the user's own process command lines returns an empty list, so a live daemon is not found and the previous refusal behavior applies

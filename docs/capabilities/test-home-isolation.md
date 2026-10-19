# `test-home-isolation`: the automated test suite neither reads nor writes the developer's real home directory, and a test that needs a user configuration provides its own

**Delivered by:** improve-iteration-4
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar clean`, `bakar clean-cache --full -y`, `bakar prefetch`, `bakar shell -c CMD`, `bakar build --on HOST KAS_YAML`, `bakar triage RUN_ID`, `bakar layers inspect --json`, `bakar hashserv status`, `bakar getvar VAR`, `bakar doctor`, `bakar stop` and `bakar ps`.
Precondition: none - no new config key, environment variable or migration; every node that takes part in a dispatch must run the same bakar (the identity check refuses a mismatch), so upgrade the dispatching and the build nodes before the next dispatch.
Success signal: exit codes and messages as listed under E2E Testing - run directories named with a UTC timestamp, the dispatch printing its own run-id, a refused launch or removal naming the peer, the marker path and the manual clear, `clean-cache --full` printing every leftover and exiting 1, JSON and daemon answers on standard output, and `bakar stop`/`bakar ps` behaving as before the split.
Silent failure: with the identity-check override a remote bakar older than this change writes local-time run ids, so the UTC fence finds no run (a host west of UTC) or a stale pre-upgrade run (a host east of UTC) without any error; and an ownership marker left by a crash on a hard-mounted shared directory still blocks peers until it is deleted by hand, now with its path in the refusal.

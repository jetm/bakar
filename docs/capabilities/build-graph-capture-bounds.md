# `build-graph-capture-bounds` - the post-build dependency-graph capture's reliability contract: a bounded wait that cannot hang a completed build, an opt-out, artifact provenance that rejects a stale capture, and a parse failure distinguishable from an empty graph

**Delivered by:** improve-reliability-and-structure
**Modules touched:** src/bakar/steps, src/bakar/graph_analyze.py, src/bakar/build_stop.py, src/bakar/user_config.py, src/bakar/config.py, src/bakar/commands, tests

## Delivery Note

Invocation: `bakar build -f <manifest> -m <machine>` (kas families only); `bakar build ... --no-capture-graph` to decline.
Precondition: none for the bounded wait or freshness check - both apply unconditionally to every kas-family build. The opt-out requires nothing beyond the flag itself; declining skips the capture and the up-to-60s cooker-idle wait entirely.
Success signal: a stalled `bitbake -g` is killed via the promoted `build_stop` escalation ladder and the build still returns with its already-reported result; stderr carries `dependency graph: capture abandoned after <bound>s` when the bound is hit. A pre-existing `task-depends.dot` older than the triggering build is refused with a named staleness warning rather than republished under this run's provenance marker. `read_graph` (`src/bakar/graph_analyze.py`) returns a value that distinguishes "parsed to empty" from "could not be parsed", read by `insights_timing.py`'s graph-join section.
Silent failure: none by construction - the bound, the opt-out, and the freshness check each report explicitly on their triggering path (a stderr warning or an explicit refusal), and `read_graph`'s two failure states no longer collapse into one ambiguous "empty" reading.

## Notes

The capture's kill escalation reuses `build_stop.py`'s existing SIGTERM-then-SIGKILL process-group ladder through a newly promoted public entry point, rather than a second, independently-maintained kill path - see design.md's "Where the capture's kill escalation comes from" decision.

The cooker-kill trade-off flagged in design.md's Open Questions was left unresolved by design: the capture's trailing `bitbake -m` still kills the warm-reconnect cooker on every successful build, and this change does not choose among the three documented options (skip the kill, accept the cold-reparse cost, or reconnect after capture). A future change should pick one before this is called closed.

This capability does not extend to the qcom family: qcom sources its environment and runs bitbake directly in a bash subshell rather than through kas, so the capture mechanism has no equivalent there and `capture_graph`/`--no-capture-graph` has no effect on a qcom build.

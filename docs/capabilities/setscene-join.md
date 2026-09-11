# `setscene-join` - restored tasks joined to the nodes they stand in for, so a seeded build's path is computable rather than refused

**Delivered by:** task-level-critical-path
**Modules touched:** src/bakar/insights_timing.py, src/bakar/commands/insights.py

## Delivery Note

Invocation: `bakar insights --timing --workspace <workspace> <run-id>` renders the `graph join:` section, printed immediately before `buildstats join:`.
Precondition: an executed task is looked up against the captured graph by its own name first; only on a miss, and only when the name ends in `_setscene`, is the suffix stripped and retried. `task-depends.dot` carries zero `_setscene` nodes by construction - `bitbake -g` graphs the work a build *can* do, never the restore variants that stand in for it - so on a seeded build most executed tasks would otherwise fail to join at all.
Success signal: `graph join NN.N% (X of Y executed tasks resolved to a graph node)`, printed on both the passing and refusing branch. Below `JOIN_RATE_THRESHOLD` (95%), the critical path refuses to publish rather than compute a chain over a partially joined graph.
Silent failure: none by construction - a run with no dependency source states `graph join unavailable: no dependency source supplied` rather than reporting a rate of zero, and a run whose join falls short names a bounded (`UNJOINED_SAMPLE`) list of the executed tasks that missed, rather than only a shortfall count.

## Notes

Measured on PC3 against run `20260910-173444`, after deploying this change's own build (`bakar 0.31.1 (a56c3e4ffa98)`, confirmed by content hash):

```text
graph join:
  graph join 99.2% (2545 of 2566 executed tasks resolved to a graph node)
  unjoined: libedit-native-20251016-3.1-r0:do_create_package_spdx_setscene
  unjoined: libedit-native-20251016-3.1-r0:do_populate_lic_setscene
  unjoined: libedit-native-20251016-3.1-r0:do_create_spdx_setscene
  unjoined: libedit-native-20251016-3.1-r0:do_create_recipe_spdx_setscene
  unjoined: libedit-20251016-3.1-r0:do_unpack
```

99.2% clears the 95% gate with 4.2 points of margin. The entire residual (21 of 2566 identities, 0.8%) is one recipe, `libedit`/`libedit-native`, whose PV contains a hyphen (`20251016-3.1-r0`) that `strip_recipe_version` does not fully reduce - a deliberately bounded gap (`devtool-debt:` marker in `insights_timing.py`, ceiling: stays inside the gate's 5% budget; upgrade trigger: a run refuses with its unjoined sample dominated by version-strip misses), not something this change fixes.

This run's regime line (`36.3% restored (833 of 2295 tasks from sstate)`) is a different statistic from the graph join's own denominator - `833 of 2295` is bitbake's own setscene-candidate count (`eventlog.py`'s runqueue `setscene_total` stat), not the 2566 executed tasks the graph join and critical path actually count against. Read against the executed-task denominator, 833 of 2566 (32.5%) were setscene restores with no node of their own in the graph, and the 99.2% join rate above accounts for nearly all of them by resolving each restore to the node of the task it stood in for. None of the top-10 chain nodes rendered in this run's `critical path:` section happened to be a restore-weighted node - the heaviest 10 tasks on this build all ran for real - so no `(via <restore-task>)` annotation appears in this particular run's output. The contributor-naming path is exercised by the unit tests in `tests/test_insights_timing.py`, not by this run.

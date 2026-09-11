# `task-level-critical-path` - the duration-weighted longest chain over the task graph, each node weighted by its own elapsed time

**Delivered by:** task-level-critical-path
**Modules touched:** src/bakar/insights_timing.py, src/bakar/graph_analyze.py, src/bakar/commands/insights.py

## Delivery Note

Invocation: `bakar insights --timing --workspace <workspace> <run-id>` renders the `critical path:` section.
Precondition: the run directory must hold a `task-depends.dot` correlated against the run's own build window (via the `captured_at` field of `dependency-graph.json`, accepted up to `GRAPH_CORRELATION_TOLERANCE_S` past the window's end) plus a `bitbake-events.json` carrying task rows, and the graph join (see `setscene-join`) must clear its 95% gate.
Success signal: the section prints a total, a node count, and up to `CRITICAL_PATH_TOP_N` (10) of the path's heaviest nodes, each named as `<pn>.<task>` rather than a bare recipe.
Silent failure: none by construction. The path is computed over the task graph directly - `collapse_to_pn` is never called here, because collapsing to recipe names is what made OE's real dependency graph cyclic and the critical path permanently unavailable before this change. Below the graph-join gate, or when the task graph itself contains a cycle, the section refuses and names which of those two conditions failed rather than substituting a recipe-level chain.

## Notes

Measured on PC3 against run `20260910-173444` (`~/repos/personal/yocto/yocto-bench/build/runs/20260910-173444`), after deploying this change's own build (`bakar 0.31.1 (a56c3e4ffa98)`, confirmed by content hash against `ssh pc3 bakar --version`, not by git commit):

```text
critical path:
  critical path: 501.7s over 112 tasks
  gnutls.do_configure: 56.0s
  libunistring.do_configure: 53.2s
  libunistring.do_compile: 44.2s
  gcc-runtime.do_compile: 43.0s
  glibc.do_compile: 41.9s
  gnutls.do_compile: 39.3s
  glibc.do_install: 36.3s
  binutils.do_compile: 22.7s
  libidn2.do_configure: 17.5s
  gcc-runtime.do_configure: 16.6s
```

Every chain entry names one task of one recipe (`gnutls.do_configure`, not `gnutls`), which is the property that makes the total comparable against the CPU floor:

```text
concurrency floor:
  concurrency floor 501.7s = max(CPU floor 371.2s, critical path 501.7s) - the critical path binds
  basis: both the critical path and the CPU floor are task-level - the path weights each node by the elapsed time of the executed task that resolved to it, the CPU floor by per-task CPU seconds / recorded cores. The two bounds share a basis, so the difference between them is a quantity a reader may reason about
```

None of this run's top-10 rendered nodes happened to be weighted by a setscene restore, even though 36.3% of the run's executed tasks (833 of 2295) were restores - the heaviest 10 tasks on this particular build were all executed for real. A restore-weighted node renders with its contributor named (see `setscene-join`); this run simply did not surface one in the bounded top 10.

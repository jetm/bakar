# `cpu-floor-bound` - per-task CPU seconds, the CPU floor, and the concurrency floor as `max(CPU floor, critical path)` with headroom against the actual build duration

**Delivered by:** insights-buildstats-source
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar insights --timing --workspace <workspace> <run-id>` renders the `cpu floor:` and `concurrency floor:` sections.
Precondition: the run's artifact must carry a `host` block recording the build host's core count, which `eventlog` writes at capture time from schema 5 onward. A run captured before that field existed reports the floor as unavailable rather than substituting the analysing host's count.
Success signal: `CPU floor 45.7s = 1461.9 joined CPU seconds / 32 cores (build host cpu_count, recorded at capture)`.
Silent failure: none - every path that cannot compute a bound degrades with a stated reason. The CPU floor refuses below the 95% buildstats-join gate; the critical path refuses below its own 95% graph-join gate; the CONCURRENCY floor refuses whenever either bound is unavailable, since it needs both.

## Why the concurrency floor can still be unavailable

This is a documented refusal on the failure paths, not a permanent gap. The critical path is computed from the run's own captured dependency graph (`task-depends.dot`, correlated against the run's build window) rather than a live `bitbake -g <recipe>` invocation, and is gated on the share of executed tasks that resolve to a graph node - below 95%, the section refuses rather than publish a chain over a partially-joined graph. `ConcurrencyFloor` is `max(CPU floor, critical path)` and needs both terms, so either gate refusing, or no dependency graph being captured for the run at all, degrades the concurrency floor to an explicit note.

Rendering the CPU floor alone under the concurrency-floor label would still be worse than refusing: a throughput bound presented as a dependency bound is exactly the error that made an 11.9% CPU-only headroom read as actionable on a build whose real headroom was 1.1%. The smoke asserts the note appears rather than a duration on that path.

## The join gate

No CPU-derived figure is published unless at least 95% of executed tasks carry a buildstats record, and the achieved rate is printed either way. The denominator counts every executed task, including one whose timestamps are missing or unparseable - such a task lowers the rate rather than vanishing from both sides of it, which is the failure mode the gate exists to catch and which it was itself briefly vulnerable to.

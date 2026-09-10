# `task-churn-columns` - `minflt`/`majflt`/`syscalls`/`GB_wr` aggregated per task type

**Delivered by:** insights-buildstats-source
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar insights --timing --workspace <workspace> <run-id>` renders the `task churn:` section.
Precondition: a buildstats capture correlated to the run. Unlike the CPU floor, churn renders over a partial join with its coverage stated - a per-task-type aggregate degrades gracefully where a build-wide bound does not.
Success signal: a table of task types with counts, e.g. `do_compile 38 tasks / 26,652,217 minflt / 997 majflt / 4,631,211 syscalls / 3.00 GB_wr`, ordered by minor faults.
Silent failure: the one to watch for is every `minflt` column reading zero. That is what a mistyped field name plus a `.get(field, 0)` fallback produces, and it looks populated. The fields are spelled `rusage ru_minflt:` and `Child rusage ru_minflt:`, NOT line-initial `ru_minflt` - a reader grepping the latter matches nothing.

## What the ratio means

The minor-to-major fault ratio separates process-churn-bound work from I/O-bound work. On a real capture the columns rank 21 task types across 3.68 orders of magnitude, from `do_deploy_source_date_epoch` at 470,240 down to `do_configure_ptest_base` at 98.

Do not expect a specific pair to separate. An earlier form of this capability asserted that `do_configure` and `do_unpack` differ by two orders of magnitude; measurement across three real captures falsified it - best separation 0.99 orders, with the sign inverted on two of the three, `do_unpack` measuring as the churn-heavy type. The pair's spread moved from -0.99 to +0.74 orders across captures, so any named-pair claim rots on a different recipe mix. The ranking property is what held everywhere.

## Counters and their scope

The fault counters sum the task's own rusage and its children's, and the child usually dominates - `do_configure` measured 7.0M self minor faults against 214.2M child, because the work happens in spawned configure and compiler subprocesses. The IO counters are self-only, because bitbake reads them from `/proc/<pid>/io` for the task process alone and no child variant exists in the file.

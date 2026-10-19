# `run-progress-snapshot`: what `bakar status --detail` reports for a run, where each value comes from, the bounds on each value and on the call, the remote form, the output streams, and what each field says when its source is missing

**Delivered by:** bakar-build-progress-snapshot
**Modules touched:** hooks, other, tests

## Delivery Note

Invocation: bakar status <run_id or unit> --detail --json [--on HOST]
Precondition: the bakar build carrying this change installed on the machine that runs the build (and on the host named by --on, same version on both); the devtool rule and nudge hook deployed with `devtool project sync`; for sstate free space, a run started by this bakar so its build_paths event exists
Success signal: exit 0 and one JSON object on stdout with the twelve status keys plus a `detail` object holding running_tasks, failed_tasks, last_line, disk_free_gb, mem_available_mb and pressure; without --json, three to five lines on stderr and nothing on stdout
Silent failure: a run without a bitbake event log reports running_tasks and failed_tasks as null with no message, a run recorded before this change reports sstate free space as null, a filesystem that does not answer in 2 seconds reports null for that value, and an agent that keeps using tail, grep and ssh never learns the command exists unless the rule is loaded

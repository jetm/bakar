# `run-status-and-wait`: the failure object of a failed run gains the `stopped` step and carries the captured cause as its error text

**Delivered by:** bakar-run-failure-reasons
**Modules touched:** other, tests

## Delivery Note

Invocation: bakar stop <kas.yml> --run <run_id>, then bakar status <run_id> --json and bakar triage <run_id> --json
Precondition: bakar with this change installed on the node that runs the build and reads the status; for bakar stop --on HOST only ssh access to the host is needed, because the host runs the generated stop script
Success signal: status reports state failed with failure.step "stopped" and failure.error "stopped by bakar stop" for a stopped run, failure.error with the printed ERROR line for a bad target, wait exits 1 for both, and triage fail_reason equals failure.error
Silent failure: runs recorded before this change, builds ended with kill or systemctl stop, and a dispatched unit stopped before it registers its run record no stop request and read as an ordinary failure with error exit_code=N

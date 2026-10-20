# `run-failure-reasons`: a doctor refusal records the blocking checks as its reason text under the same one-line bound, status and triage report it through the existing precedence, the text form of triage prints any recorded reason as written, and agents are taught what the step `doctor` means

**Delivered by:** bakar-doctor-failure-reasons
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build <kas.yml> --target <recipe> --detach` on a build the doctor step refuses, then `bakar status <handle> --json` and `bakar triage <run_id> --json` (or `bakar triage <run_id>` for the text form)
Precondition: bakar with this change installed on the node that runs the build and reads the status; for a build dispatched with --on HOST the host's bakar must contain it too, because the host's doctor step records the event; a blocking doctor check must fail, for example another build holding bitbake.lock in the same build directory
Success signal: bakar wait exits 1; status reports state failed with failure.step "doctor" and failure.error naming each blocking check as `<check>: <message>` joined by "; " within 200 characters, for example "bitbake-locks: bitbake.lock held by PID 4242 - another build is running"; triage reports failing_step "doctor" and an equal fail_reason, and its text form prints that text with square brackets intact
Silent failure: a run recorded before this change, or by a build host that runs an older bakar, still reads "blocking failure" with no check named, and an entry shortened to fit the bound ends with "..." and hides the rest of its message, which stays in diagnosis.txt in the run directory

# `agent-next-step-hints`: the wait line printed after a detached launch says to run the wait in the background

**Delivered by:** bakar-follower-and-run-record-fixes
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar log --on <host> --run <run_id> --lines 50 --no-follow` to read the end of a log, and `bakar log --on <host> --run <run_id>` to follow it; `bakar status <run_id> --on <host> --json` for elapsed time; `bakar ps --on <host> --json` for the machine name
Precondition: bakar with this change installed on the workstation and on every host that runs the command, because the host runs its own `bakar log`, `status` and `ps`; the live check needs a finished run on a host and the hosts synced to the new version
Success signal: `--no-follow` prints the requested lines and exits 0; Ctrl-C or `| head` on a follow ends the local command and the remote follower within a few seconds; `elapsed_seconds` of a finished run no longer changes between calls; `ps` rows name the real machine
Silent failure: a host on an older bakar rejects `--lines` and `--no-follow` with a usage error on stderr and an empty stdout; a network partition without a TCP close leaves the follower until sshd's keepalive fires; runs started before the change show `unknown` for the machine, and a container run from before the change still translates through the layout fallback

# `disk-free-check`: the failure message names the threshold the check compared against

**Delivered by:** bakar-on-worktree-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build kas/machine/raspberrypi5.yml --target python3-lgpio --on pc2 --yes` run from `/home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed`
Precondition: ssh alias `pc2` reachable with BatchMode; the new bakar installed on both nodes (identity-parity check refuses a mismatch); `/home/tiamarin/repos/work/peridio` present locally; the worktree registered in that checkout's `.git/worktrees`
Success signal: exit 0 from the remote build; the dispatch plan names only additive remote writes (since bakar-on-release-workspace-dispatch, nothing is deleted on the remote); doctor rows `kas-yaml-syntax` and `host-preflight` PASS; remote run id printed
Silent failure: a delivered worktree left on an older commit when its ref push fails part-way; the dispatch reports the failed push and does not start the build, so check the exit code rather than the remote directory's presence

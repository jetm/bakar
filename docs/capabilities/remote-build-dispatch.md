# `remote-build-dispatch`: the mirror requirement gains a second, scoped mirror root for a worktree checkout outside the workspace root, transferred after the workspace root; the preview and confirmation cover every root; a new requirement refuses a mirror root that contains linked worktrees

**Delivered by:** bakar-on-worktree-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build kas/machine/raspberrypi5.yml --target python3-lgpio --on pc2 --yes` run from `/home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed`
Precondition: ssh alias `pc2` reachable with BatchMode; the new bakar installed on both nodes (identity-parity check refuses a mismatch); `/home/tiamarin/repos/work/peridio` present locally; the worktree registered in that checkout's `.git/worktrees`
Success signal: exit 0 from the remote build; exactly two `rsync --delete preview` lines (main workspace, then worktree); doctor rows `kas-yaml-syntax` and `host-preflight` PASS; remote run id printed
Silent failure: a mirrored worktree whose `gitdir:` pointer targets a `.git/worktrees/<name>` entry the remote's main `.git` lacks, which git reports only when kas or doctor first touches it; this happens if the workspace-root transfer is skipped or ordered after the worktree transfer

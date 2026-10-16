# `remote-build-dispatch`: dispatch itself never deletes anything on the remote (`--clean`, `KAS_TASK` and clean-task targets are refused with `--on`); a meta-avocado build delivers the layer under change as a git worktree of the remote release workspace's `meta-avocado` clone (pushed to a new `refs/bakar/<name>/<commit12>` ref, uncommitted state as a snapshot commit) and runs the remote build from it; other build families keep an add-and-update-only mirror; the delete preview and its confirmation are gone and `--yes` only overrides a bakar version mismatch

**Delivered by:** bakar-on-release-workspace-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `cd /home/tiamarin/repos/work/peridio && bakar build /home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed/kas/machine/raspberrypi5.yml --target python3-lgpio --on pc3`
Precondition: ssh alias `pc3` reachable with BatchMode; the new bakar installed on both nodes (identity-parity check refuses a mismatch); `[workspaces] template = "/home/tiamarin/repos/work/peridio-{release}"` in `~/.config/bakar/config.toml`; `/home/tiamarin/repos/work/peridio-wrynose` present on pc3 with a `meta-avocado` git clone and an `openembedded-core` declaring `wrynose`
Success signal: exit 0 from the remote build; a `dispatch plan -> pc3` block whose lines all start with `+` (the delivered worktree under `/home/tiamarin/repos/work/peridio-wrynose/.bakar/worktrees/rpi5-lgpio-feed`, the `refs/bakar/rpi5-lgpio-feed/<commit12>` ref, the build directory `build-raspberrypi5-rpi5-lgpio-feed`) followed by `nothing is deleted on the host`; no confirmation prompt; remote run id printed; a listing of `/home/tiamarin/repos/work/peridio-wrynose` on pc3 taken before and after differs only by those additions
Silent failure: a push that lands but a remote `git gc` or a forced ref update removes an earlier `refs/bakar/*` ref or its objects, so an older delivered worktree loses its commit; nothing in the dispatch output reports it, and it shows only when that worktree is next checked out or built

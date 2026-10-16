# `release-workspace-routing`: derive a meta-avocado build's Yocto release from the `openembedded-core` pin in its merged kas configuration without touching any repository, map the release to a workspace through `[workspaces] template` or `[workspaces.releases]` in the user-global bakar config, build there locally and remotely, verify a remote workspace's existence, `meta-avocado` clone and declared series before dispatch writes to it, and refuse `--on` with no mapping configured

**Delivered by:** bakar-on-release-workspace-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `cd /home/tiamarin/repos/work/peridio && bakar build /home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed/kas/machine/raspberrypi5.yml --target python3-lgpio --on pc3`
Precondition: ssh alias `pc3` reachable with BatchMode; the new bakar installed on both nodes; `[workspaces] template = "/home/tiamarin/repos/work/peridio-{release}"` in `~/.config/bakar/config.toml`; the worktree's kas files pin `openembedded-core` at `branch: wrynose-avocado`; `/home/tiamarin/repos/work/peridio-wrynose` present locally and on pc3
Success signal: exit 0 from the remote build; the dispatch plan names `/home/tiamarin/repos/work/peridio-wrynose`, not `/home/tiamarin/repos/work/peridio`; no `remote workspace check failed` line; with the `[workspaces]` table removed the same command exits non-zero naming `[workspaces] template` and runs no ssh, rsync or git command
Silent failure: a per-release `[workspaces.releases]` entry or a stale template that points at a workspace whose `meta-avocado` clone belongs to another release but whose `openembedded-core` still declares the expected series; routing and the remote check both pass, and the build mixes layers from two trees with no error until a recipe or layer compatibility check fails deep in the build

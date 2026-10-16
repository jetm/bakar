# `kas-worktree-resolution`: a meta-avocado kas file in a linked worktree resolves its workspace from the main checkout unless release routing resolves one, in which case the routed release workspace wins; the kas wrapper declares meta-avocado by the checkout's absolute path whenever that checkout is not the workspace's own `meta-avocado` (a linked worktree, or a main checkout outside a routed workspace); the doctor validates through the same wrapper; a worktree build gets a build directory named after the worktree

**Delivered by:** bakar-on-release-workspace-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `cd /home/tiamarin/repos/work/peridio && bakar build /home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed/kas/machine/raspberrypi5.yml --target python3-lgpio --on pc3`
Precondition: ssh alias `pc3` reachable with BatchMode; the new bakar installed on both nodes; `[workspaces] template = "/home/tiamarin/repos/work/peridio-{release}"` in `~/.config/bakar/config.toml`; the worktree registered in `/home/tiamarin/repos/work/peridio/meta-avocado/.git/worktrees`; `/home/tiamarin/repos/work/peridio-wrynose` present locally and on pc3
Success signal: exit 0 from the remote build; doctor rows `kas-yaml-syntax` and `host-preflight` PASS; the build runs in `/home/tiamarin/repos/work/peridio-wrynose/build-raspberrypi5-rpi5-lgpio-feed`, not in the shared per-machine build directory; the generated wrapper's `repos.meta-avocado.path` is the delivered worktree's absolute path
Silent failure: a wrapper that falls back to the workspace's own `meta-avocado` clone instead of the worktree path; kas then builds the release workspace's pins rather than the worktree's, the build succeeds, and the only trace is a recipe version or a `python3-lgpio` change that never reaches the image

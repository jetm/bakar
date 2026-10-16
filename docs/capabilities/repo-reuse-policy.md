# `repo-reuse-policy`: under `BAKAR_REPO_POLICY=reuse` (the default for every routed meta-avocado build and always forwarded by `--on`), classify each pinned repository of the merged kas configuration as frozen (at its pin and clean, so a generated overlay sets `url: null` and kas performs no version-control operation on it), missing (kas clones it at its pin) or side (off its pin or dirty, so kas is pointed at a side checkout under `<workspace>/.bakar/pins/<repo>/<commit12>` that bakar never modifies), so a build in a shared release workspace never moves an existing clone

**Delivered by:** bakar-on-release-workspace-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: `cd /home/tiamarin/repos/work/peridio && bakar build /home/tiamarin/repos/work/.worktrees/meta-avocado/rpi5-lgpio-feed/kas/machine/raspberrypi5.yml --target python3-lgpio`, from a worktree whose kas files pin `meta-raspberrypi` at a commit other than the one checked out in `/home/tiamarin/repos/work/peridio-wrynose/meta-raspberrypi`
Precondition: `[workspaces] template = "/home/tiamarin/repos/work/peridio-{release}"` in `~/.config/bakar/config.toml`; `BAKAR_REPO_POLICY` unset in the shell (routing then sets it to `reuse`); every pinned repository with a `url` also carries a `commit`
Success signal: stderr prints `side checkout meta-raspberrypi at <commit12>: /home/tiamarin/repos/work/peridio-wrynose/.bakar/pins/meta-raspberrypi/<commit12>` on the first build; `git -C /home/tiamarin/repos/work/peridio-wrynose/meta-raspberrypi rev-parse HEAD` prints the same commit before and after the build; the build directory holds `avocado-reuse.yml` with `url: null` for every frozen repository; a second build reuses the side checkout and prints no new side line
Silent failure: a kas file outside any meta-avocado checkout under `BAKAR_REPO_POLICY=reuse` falls back to the kas policy, announced by one `repo policy reuse skipped` line on stderr that is easy to miss in a long build log; any unknown policy value is refused rather than treated as `kas`

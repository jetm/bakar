# `nfs-workspace-refusal`: its first scenario no longer assumes a local `TMPDIR` can sit beside an NFS workspace through a setting; it says the workspace is refused whatever the `TMPDIR` is. The requirement text is unchanged

**Delivered by:** remove-local-tmpdir-base
**Modules touched:** other, tests

## Delivery Note

Invocation: any bakar command that loads the user config, for example `bakar doctor --workspace ~/repos/work/peridio-wrynose`, run against a `~/.config/bakar/config.toml` that still sets `[build] local_tmpdir_base`; and `bakar clean` and `bakar triage` run from a workspace
Precondition: the bakar release carrying this change is installed; each host's `~/.config/bakar/config.toml` should no longer set `[build] local_tmpdir_base` (pc2's configuration source already dropped it and the removal takes effect at that host's next chezmoi apply)
Success signal: a config that still sets the key prints one `unknown key 'local_tmpdir_base' in [build]` warning per command and the command proceeds; the build TMPDIR is `<build dir>/tmp` (`tmp-glibc` for qcom) and the generated local.conf has no `TMPDIR` line; `bakar clean` removes the build directory with its tmp and exits 0; `bakar triage` reports a log outside the workspace as "logfile outside workspace, not read"
Silent failure: a host that really kept TMPDIR on a second device and still has the key in its config gets only the warning, and its build TMPDIR moves onto the workspace device with no further message, because the disk-free check reports a device only once it is already below its threshold; a leftover base directory is never cleaned or reported

# Release workspaces

This feature keeps one workspace per release. A meta-avocado kas build is routed to the workspace for the release its pins name, using a path template with a `{release}` placeholder. The routing rules and the failure it guards against are described in [release-workspace-routing.md](../capabilities/release-workspace-routing.md).

## What your host needs

1. Choose an absolute path template with a `{release}` placeholder, for example `/home/me/repos/work/peridio-{release}`. `~` is expanded. Any other placeholder, a format spec, a conversion or a relative path is a configuration error.
2. Create the workspace for each release you build locally at the path the template produces, for example `/home/me/repos/work/peridio-wrynose`. A local copy matters only for local routed builds. A build dispatched with `--on` needs the workspace on the remote host, not here.
3. Give `bakar init` the release whose local workspace you expect, then create that directory yourself: init only checks that it exists and never creates it. The catalog lists the local release workspace as a soft requirement, so a missing copy is reported without blocking.

## Enable

Interactive: run `bakar init` and select the release-workspaces feature.

Non-interactive: `bakar init --feature release-workspaces --set workspaces.template=/home/me/repos/work/peridio-{release}`

The feature writes this key, and `--set` accepts only this one for it:

- `workspaces.template` (release workspace path template; required)

`[workspaces.releases]` overrides stay hand-edited in `config.toml`. Each maps one release to an absolute path and wins over the template. `bakar settings` does not list that table.

## Turn off

Run `bakar init --disable release-workspaces`. It unsets `workspaces.template` in your user config. It does not remove any workspace directory, and any `[workspaces.releases]` entries you wrote stay in place.

## See also

- [release-workspace-routing.md](../capabilities/release-workspace-routing.md)
- [config-reference.md](../config-reference.md#workspaces---release-workspaces-for-meta-avocado)
- [remote-dispatch.md](remote-dispatch.md)

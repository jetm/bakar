# Remote dispatch

The remote-dispatch feature sends a build to another host with `bakar build <kas> --on <host>`. For a meta-avocado build, the host builds in its own release workspace and the layer you changed is delivered to it through git. The deep page is [remote build dispatch](../capabilities/remote-build-dispatch.md).

## What your host needs

1. Working ssh to the host, with the same bakar installed on both hosts (hard requirement, probe `remote-host`). This command must succeed without a prompt: `ssh -o BatchMode=yes <host> bakar --version`. If the two bakar builds differ, `bakar init` refuses the feature (`--yes` there only authorizes sudo and does not override it), so install the same bakar on both hosts first. `bakar build --on` has its own `bakar mismatch` refusal, which its `--yes` overrides.
2. The release-workspaces feature, which maps a release to a workspace path through `[workspaces] template`. The catalog lists it as a dependency. See [release-workspaces](release-workspaces.md).
3. The release workspace on the remote host (soft requirement, probe `remote-release-workspace`). Dispatch never creates it. When it is missing, dispatch prints these one-time commands to run on the host, where `<ws>` is the workspace path your template gives for the release:

   ```bash
   mkdir -p <ws>
   git clone --branch <release> <meta-avocado origin URL> <ws>/meta-avocado
   env KAS_WORK_DIR=<ws> kas checkout <ws>/meta-avocado/kas/machine/<machine>.yml
   ```

   After `kas checkout`, `<ws>/openembedded-core/meta/conf/layer.conf` must declare the release in `LAYERSERIES_CORENAMES`; the read-only workspace check refuses otherwise.

## Enable

Interactive: run `bakar init` and select the remote-dispatch feature.

Non-interactive: `bakar init --feature remote-dispatch --set remote-dispatch.host=<alias>`

`host` is the ssh alias and is required. `release` is an optional input naming the release workspace on that host (`--set remote-dispatch.release=<release>`). Neither is written to config. The optional keys the feature writes are `dispatch.snapshot_untracked_cap_mb`, `dispatch.worktree_root` and `dispatch.layer_repo_dir`.

## Turn off

The catalog declares no keys for `bakar init --disable remote-dispatch` to unset. To change a `dispatch.*` value you set, use `bakar settings`. Dispatch itself runs only when you pass `--on`.

## See also

- [remote build dispatch](../capabilities/remote-build-dispatch.md)
- [release-workspaces](release-workspaces.md)
- `bakar build <kas> --on <host> --dry-run`

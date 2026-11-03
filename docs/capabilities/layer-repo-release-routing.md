# layer-repo-release-routing: release derivation from the dependency pin and mapping to a release workspace for local and remote builds

**Delivered by:** bakar-on-layer-repo-dispatch
**Modules touched:** other, tests

## Delivery Note

Invocation: bakar build kas/machine/jetson-agx-orin-zedx.yml --target mc:jetson-l4t:stereolabs-zedx --on pc3 --detach, run from a checkout of the layer repository; add --dry-run to preview, or run bakar build `<same kas file>` -w `<routed workspace>` for a local routed build.
Precondition: `[workspaces] template` set in ~/.config/bakar/config.toml; the host's release workspace exists with a meta-avocado clone holding the entry's pinned commit and an openembedded-core that declares the entry's dependency branch as its series; the host's bakar carries this change (bakar settings get dispatch.layer_repo_dir succeeds there); the entry declares its own repository by path only and meta-avocado with url, branch and commit; ssh key access to the host.
Success signal: the plan's first line names the routed workspace and the configuration entry that chose it; with --detach a run handle with the remote run id is printed; bakar triage and bakar status read the run from the layer build directory on the host; a --dry-run exits 0 with "dry run: nothing was pushed or created on pc3".
Silent failure: BAKAR_REPO_POLICY=kas in the environment switches the shared-clone protection off with no message, so a layer build in a release workspace can move that workspace's clones; and an entry that declares its own repository with a URL is not recognised and takes the generic path with no message saying it was not treated as a layer entry.

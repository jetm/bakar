# `kas-worktree-resolution`: the pin of the checkout stays authoritative over every overlay in every kas dump bakar runs, and a build from the workspace's own clone stays unchanged

**Delivered by:** bakar-kas-overlay-repo-path
**Modules touched:** other, tests

## Delivery Note

Invocation: `BAKAR_REPO_POLICY=reuse bakar dump kas/machine/qemux86-64.yml:kas/feature/cve-check.yml`, run from the root of a linked worktree of meta-avocado; it merges the same stack as `bakar build kas/machine/qemux86-64.yml:kas/feature/cve-check.yml --on <host> --detach`
Precondition: kas on PATH; the kas file's checkout is not the workspace's own `meta-avocado` clone (a linked worktree, or a main checkout outside a routed release workspace); for a dispatched build, the build host runs a bakar that includes this change
Success signal: `bakar dump` exits 0 and prints YAML whose `repos.meta-avocado.path` is the checkout's absolute path and whose `repos.meta-avocado.layers` holds both the machine file's layers and the overlay's (`meta-avocado-sbom` for `cve-check.yml`); `<build dir>/avocado-pin.yml` sits beside the wrapper; a dispatched build starts instead of ending with `Configuration file not found` under `bakar-kas-pins-work-`
Silent failure: a build host that still runs an older bakar assembles its own dump, so a routed build there keeps failing with the temporary-directory message and a pinned build under the `kas` policy there keeps building the workspace's own clone of `meta-avocado` without an error

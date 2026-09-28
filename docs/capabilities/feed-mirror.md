# `feed-mirror`: populating the local package feed from a remote published feed - repository selection, integrity verification, write confinement, ownership guard, resume, disk-space preflight, and publication order

**Delivered by:** feed-mirror-command
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar feed mirror <url> --target <machine> [--release 2024] [--channel edge] -w <workspace>` (exact flag surface is Open Question 3 above; command lands in `src/bakar/commands/feed.py` as a new `@feed_app.command("mirror")`)
Precondition: a bakar workspace resolves at `-w` (or the CWD); `createrepo_c` is not required for mirroring itself (only for `sync`), but `feed doctor` will still gate on it - confirm in design whether `mirror` gets its own preflight tier
Success signal: `<feed_root>/<release>/<channel>/{sdk,target}/<repo>/repodata/repomd.xml` exists and every referenced `_pkgs/<aa>/<sha>.rpm` is present with a matching sha256; exit code 0
Silent failure: a partial download that leaves a `repomd.xml` referencing pool files that were never fetched - this is exactly what the "write pointers/index last" discipline is meant to prevent, and must have a task-level test asserting it

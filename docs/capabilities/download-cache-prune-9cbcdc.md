# `download-cache-prune`: the apply report names the link command when it removed hub-served files and mirror directories are configured

**Delivered by:** dl-mirror-link-restore
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar doctor --workspace ~/repos/work/peridio` (and every `bakar build` pre-flight) links missing mirror files automatically; `bakar cache link-mirror` shows a dry run and `bakar cache link-mirror --apply` links on demand
Precondition: the user config `[build]` sets `dl_mirror_paths` to the mounted hub directories (downloads first, then incoming) and `dl_dir` (or the `DL_DIR` environment variable) names the local downloads directory; at least one mirror directory passes the readiness probe; the build is host mode (the verb also refuses when the user config selects the container build)
Success signal: the `dl-mirror-links` row reports it linked N files (INFO) and each linked name is a symbolic link in the downloads directory pointing at its configured mirror path; a rerun reports nothing to link; the verb exits 0 with `linked N file(s)` and `applied`; a build directory with a current `do_fetch` stamp then unpacks the source instead of failing
Silent failure: a stale-stamp source that is a git or gitsm recipe, or that a fetcher stores in a subdirectory, is never linked, and the report counts only git mirror tarballs, so such a build still fails at unpack with no mention from the row or the verb; a link whose mirror file later disappears dangles until BitBake's next fetch replaces it

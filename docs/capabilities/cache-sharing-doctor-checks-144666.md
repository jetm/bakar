# `cache-sharing-doctor-checks`: the requirement that the link row passes at informational severity when there was nothing to link is narrowed to a mirror that holds files; a row whose listed directories are all empty reports at warning severity and names them, and a partly empty mirror names the empty ones beside the normal result

**Delivered by:** dl-mirror-link-hardening
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar cache link-mirror` (dry run) or `bakar cache link-mirror --apply`, and `bakar doctor --workspace ~/repos/work/peridio` (the row also runs in every `bakar build` pre-flight), on a host-mode host whose `[build] dl_mirror_paths` names a mirror directory that lists no regular files
Precondition: the bakar release carrying this change is installed; a host-mode build with `[build] dl_mirror_paths` set and a downloads directory configured through `DL_DIR` or `[build] dl_dir`; the mirror directory passes the readiness probe, meaning the path answers
Success signal: the verb's report has an `empty: <dir> (lists no regular files, as a share that is not mounted does)` line beside its usual counts and still exits 0 with 0 to link; the `dl-mirror-links` row shows a warning naming the directory when every listed directory is empty, and an informational result that names the empty directory when another directory holds files
Silent failure: a stale or partial mount that still lists some files is not reported as empty; svn, cvs and bzr tarballs stamped on the hub are linked without a checksum check, a run has no cap on links and a dangling link is never removed, and none of the three prints anything

# `buildstats-discovery` - locate the buildstats tree for a run and report honestly when it is absent, partial, or from a different build than the event log

**Delivered by:** insights-buildstats-source
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar insights --timing --workspace <workspace> <run-id>` renders the `buildstats join:` section, which names the capture directory it correlated against.
Precondition: the run's persisted `bitbake-events.json` must carry task timestamps, from which the correlation window is derived. A run whose artifact records none reports `uncorrelated` rather than guessing.
Success signal: the section prints a join percentage and the capture path, e.g. `buildstats join 100.0% (433 of 433 executed tasks matched a buildstats record). from capture <path>`.
Silent failure: none by construction - the four discovery outcomes are kept distinct on purpose. `absent` means no tree at the path, `empty` means a tree holding no captures, `uncorrelated` means captures exist but none falls in this run's build window, and only `parsed` publishes numbers. Collapsing any two of these was the defect this capability exists to prevent: a tree that was never found and a build that recorded nothing call for opposite responses.

## Notes

The tree is an INPUT, not something bakar produced: builds run on remote nodes and sstate is shared over NFS, and `--workspace` comes from the caller. The reader therefore rejects symlinked capture directories and task files and caps per-file size, the way a config parser treats a config file.

Capture correlation reads the directory name as UTC and accepts its mtime as a second signal. The two are not interchangeable - the name is the build's start, the mtime is near its end (measured 14:20:35 against 14:26:30 on a six-minute capture) - so the name is preferred wherever it correlates and an ambiguous mtime-only match is refused rather than guessed.

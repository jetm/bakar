# `native-signature-ledger`: preservation of executed native/cross task signature files beyond the life of the build directory

**Delivered by:** native-miss-forecast
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar insights --natives [RUN_ID]` explains a finished run; the record and ledger are written by every `bakar build` and `bakar bitbake`, and `bakar doctor` runs the `native-rebuild-forecast` check
Precondition: bakar built from this change running on the node that builds (for validation, an isolated copy on PC3 run through `uv run --project`; a release install must have the same identity on every node used with `--on`); an effective sstate directory (`SSTATE_DIR` or `[build] sstate_dir`); a bitbake checkout in the workspace for `--natives`
Success signal: `<sstate_dir>/.bakar/native-provenance/<release>/<digest>.json` and `<sstate_dir>/.bakar/native-sigdata/<recipe>/<task>.<hash>.sigdata` appear after a build, the run directory holds `native-signatures.json`, and `bakar insights --natives` prints four bucket counts that sum to the rebuilt native/cross recipe count
Silent failure: with no effective sstate directory the record write and ledger capture are skipped without any message, so `native-rebuild-forecast` reports skipped at INFO and `bakar insights --natives` reports the run has no signature manifest; a run made before this change also has none

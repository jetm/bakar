# `feature-requirement-checks`: The new doctor checks and the doctor runner's named-subset and deadline support

**Delivered by:** init-feature-catalog
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar init` (interactive wizard), `bakar init --feature <id> [--set KEY=VALUE] -w <dir> [--yes]` (non-interactive), `bakar init --explain <feature>` (read-only requirements guide)
Precondition: a TTY for the wizard; per feature, its own host requirements (listed on its docs/features page); `--yes` plus passwordless sudo for any non-interactive run whose plan needs sudo; questionary 2.0 or later, already a dependency
Success signal: exit 0, the plan lists every selected feature and host remediation, the chosen keys are written to `~/.config/bakar/config.toml` (family defaults to `<workspace>/.bakar.toml`), and `checks: N/M run` shows N equal to M; a following `bakar doctor` reports the selected Tier 1 and Tier 2 features' checks as PASS (the central checks run only because central-node and central-client set `cluster`, which is an explicit acceptance test), while Tier 3 features additionally need the remote host prepared per the guide
Silent failure: a requirement check that produces no result (misspelled name or crash) reading as met, which the `checks: N/M run` line and a catalog test guard against; or a feature whose config key is written but whose gating key is not (for example `bb_hashserve` set without `cluster`), so doctor skips the central checks and the build proceeds on an unreachable endpoint

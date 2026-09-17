# `stop-host-wide-fallback`: When workspace resolution from the current directory finds no workspace, the stop command discovers live host-mode builds across the whole host instead of failing, and gates any resulting signal behind explicit operator confirmation

**Delivered by:** stop-host-wide-fallback
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar stop` or `bakar stop --run <run-id>`, run from any directory including one outside any BSP workspace
Precondition: none - the fallback engages automatically whenever workspace resolution finds nothing and no explicit `-w`/`--workspace` was given
Success signal: exit 0, the targeted host-mode build's process group receives the stop signal, and a subsequent `bakar ps --json` no longer lists that run id
Silent failure: none - every path (no live build, ambiguous selection, ownership refusal, declined confirmation, invalid explicit workspace) prints an explicit message and exits non-zero

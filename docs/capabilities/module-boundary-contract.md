# `module-boundary-contract`: which module owns which symbol after the split, and the re-export policy that governs what the origin module continues to expose

**Delivered by:** improve-god-modules
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build -f <kas.yml>`, `bakar doctor -f <manifest>`, `bakar monitor`,
`bakar cluster-info` - the same commands as before. This change delivers no new
capability; it delivers the same capability at a maintainable module size.
Precondition: none. Both previously-declared blockers (`improve-tooling-and-docs`,
`improve-cohesion-deadcode`) are archived, and the signature-narrowing precondition the
old handoff named was dropped as impossible rather than completed.
Success signal: `uv run pytest --no-cov -q` reports at least 3445 passed and at most 7
skipped; `uv run ruff check src/ tests/` is clean; `diagnostics.py` carries fewer than 85
top-level defs; `commands/build.py` is under 800 lines; all three `--help` smokes exit 0.
Silent failure: a stale test patch aimed at a relocated symbol. If the origin module
re-exports the symbol, the patch succeeds and installs a value nothing reads, so the test
runs against real behaviour while appearing isolated - in the leak-scan suite that means
the real `_read_elf` scanning an empty temporary tree, finding nothing, and passing its
"no leak" assertion for the wrong reason. The no-re-export rule and
`tests/test_module_boundaries.py` exist solely to convert that into a loud failure.

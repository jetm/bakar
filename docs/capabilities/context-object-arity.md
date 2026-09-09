# `context-object-arity`: which functions must take a context object, and the guarantee that the values they receive equal the ones the invoking command was given

**Delivered by:** improve-high-arity-signatures
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar build`, `bakar sync`, `bakar getvar`, `bakar clean-cache`,
`bakar stress-parse`, `bakar stop` - the same commands with the same flags as before. This
change delivers no new capability; it delivers the same capability with signatures a reviewer
can hold.
Precondition: `improve-god-modules` archived, and `src/bakar/commands/_build_flavors.py`
present on disk.
Success signal: every shimmed command's `--help` byte-identical at a pinned width; the suite at
or above its pre-change baseline; `uv run ruff check src/ tests/` clean; every repacked
function taking its context parameter, checked by signature inspection in
`tests/test_arity_boundaries.py`.
Silent failure: a transposed or dropped field in a packed context. The command still runs,
`--help` is unchanged and the suite stays green, because nothing in the existing suite observes
the packed values - which is why each task must ADD an observation and prove it fails when a
field is removed.

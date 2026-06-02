# Releasing bakar

## One-time setup

Complete these once before the first release:

1. **Make the repo public on GitHub** (Settings → Danger Zone). The PyPI Trusted Publisher OIDC flow requires a public repository.
2. **Set GitHub description and topics.** Description: `NXP i.MX and TI Sitara BSP build orchestrator powered by kas`. Topics: `yocto`, `bsp`, `kas`, `bitbake`, `embedded-linux`, `nxp-imx`, `ti-sitara`, `python`.
3. **Create the `release` GitHub Actions environment.** Settings → Environments → New environment → name `release`. Add yourself as a required reviewer so publish jobs pause for manual approval before pushing to PyPI. This scopes the Trusted Publisher OIDC token to a single environment instead of binding it to the whole repo.
4. **Configure PyPI Trusted Publisher.** On <https://pypi.org/manage/account/publishing/> register:
   - PyPI Project Name: `bakar`
   - Owner: `jetm`
   - Repository name: `bakar`
   - Workflow name: `publish.yml`
   - Environment name: `release`

## Per-release checklist

1. Run `devtool release patch|minor|major`. It enforces working-tree, branch, sync, and changelog preconditions, runs the full validation suite (pytest, ruff, ty, build, twine, mado), then bumps the version with `uv version`, commits, tags, and pushes atomically. Pass `--dry-run` to validate without bumping.
2. Verify:
   - GitHub Actions publish workflow on the tag passes.
   - PyPI project page shows the new version: <https://pypi.org/project/bakar/>
   - `uv tool install bakar==X.Y.Z` from a fresh shell succeeds.
   - The GitHub Release page for the tag shows the CHANGELOG section as release notes.

## Versioning policy

- `0.0.x`: pre-release development.
- `0.1.0`: first public release.
- `0.x.y`: pre-1.0 releases. Minor bumps for new capabilities; patch bumps for fixes.
- `1.0.0`: stable API.

## Do not

- Create tags manually (`git tag vX.Y.Z`). Use `devtool release`.
- Edit version strings manually. `devtool release` bumps `pyproject.toml` and `src/bakar/__init__.py` together via `uv version`.
- Push tags separately from commits. `devtool release` pushes with `--follow-tags`.

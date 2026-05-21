# Releasing bspctl

## One-time setup

Complete these once before the first release:

1. **Make the repo public on GitHub** (Settings → Danger Zone). The PyPI Trusted Publisher OIDC flow requires a public repository.
2. **Set GitHub description and topics.** Description: `NXP i.MX and TI Sitara BSP build orchestrator powered by kas`. Topics: `yocto`, `bsp`, `kas`, `bitbake`, `embedded-linux`, `nxp-imx`, `ti-sitara`, `python`.
3. **Create the `release` GitHub Actions environment.** Settings → Environments → New environment → name `release`. Add yourself as a required reviewer so publish jobs pause for manual approval before pushing to PyPI. This scopes the Trusted Publisher OIDC token to a single environment instead of binding it to the whole repo.
4. **Configure PyPI Trusted Publisher.** On https://pypi.org/manage/account/publishing/ register:
   - PyPI Project Name: `bspctl`
   - Owner: `jetm`
   - Repository name: `bspctl`
   - Workflow name: `publish.yml`
   - Environment name: `release`

## Per-release checklist

1. Confirm working tree is clean: `git status`.
2. Run the local validation suite:
   ```
   uv run pytest
   uv run ruff check src/ tests/
   uv run ruff format --check src/ tests/
   uv run ty check src/
   uv build && uvx twine check dist/*
   ```
3. Update `## [Unreleased]` in `CHANGELOG.md` with the changes for this release.
4. Commit the changelog update with `devtool commit`.
5. Bump the version with bump-my-version:
   - Patch: `uv run bump-my-version bump patch`
   - Minor: `uv run bump-my-version bump minor`
   - Major: `uv run bump-my-version bump major`

   This rewrites `pyproject.toml`, `src/bspctl/__init__.py`, and `CHANGELOG.md`, then creates a `vX.Y.Z` tag.
6. Push the commit and tag: `git push origin main --follow-tags`.
7. Verify:
   - GitHub Actions publish workflow on the tag passes.
   - PyPI project page shows the new version: https://pypi.org/project/bspctl/
   - `uv tool install bspctl==X.Y.Z` from a fresh shell succeeds.
   - The GitHub Release page for the tag shows the CHANGELOG section as release notes.

## Versioning policy

- `0.0.x`: pre-release development.
- `0.1.0`: first public release.
- `0.x.y`: pre-1.0 releases. Minor bumps for new capabilities; patch bumps for fixes.
- `1.0.0`: stable API.

## Do not

- Create tags manually (`git tag vX.Y.Z`). Use `bump-my-version`.
- Edit version strings manually. `bump-my-version` keeps the three locations consistent.
- Push tags separately from commits. Use `--follow-tags`.

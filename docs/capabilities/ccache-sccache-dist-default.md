# ccache-sccache-dist-default: the resolved `[build] ccache` value follows a new rule when no tier (env, workspace `.bakar.toml`, global `config.toml`, or the `--sccache-dist` CLI flag) explicitly sets it - it defaults to the resolved `sccache_dist` value instead of a flat `False`, so `--sccache-dist` builds (config-tier or CLI-flag-driven) keep their local-cache tail for non-allowlisted recipes without needing an explicit `ccache = true`

**Delivered by:** ccache-tristate-user-config
**Modules touched:** other, tests

## Delivery Note

Invocation: bakar build --sccache-dist -f \<manifest\>, run in a workspace whose config carries no [build] sccache_dist or [build] ccache setting at any tier
Precondition: no BAKAR_CCACHE env var and no workspace .bakar.toml [build] ccache override (both still win over the sccache_dist-conditional default, same precedence as before this change)
Success signal: BuildConfig.ccache resolves to True whether sccache-dist was activated via config.toml, .bakar.toml, BAKAR_SCCACHE_DIST, or the --sccache-dist CLI flag - all four now flow through the same resolve()-time fallback, so the ccache tuning overlay is co-selected and sccache.bbclass's allowlist-trimmed recipes get their local-cache tail back regardless of invocation style
Silent failure: a host running --sccache-dist (by any of the four means) with no explicit ccache setting anywhere gets ccache silently turned back on by this change with no warning printed - anyone who came to rely on the ccache-default-disabled change's regression (no local cache at all under sccache-dist) sees that behavior silently reverse

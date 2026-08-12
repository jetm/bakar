# bakar doctor

Run every diagnostic check and report PASS/WARN/BLOCK status. Exits non-zero when any BLOCK-severity check fails.

## Synopsis

```text
bakar doctor [KAS_YAML] [OPTIONS]
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--manifest` | `-f` | Manifest filename for BSP family dispatch |
| `--workspace` | `-w` | Workspace root override |
| `--json` | `-j` | Output results as JSON instead of the formatted table |
| `--post-build` | | Also run checks that inspect a finished build's native tree (off by default) |

## Examples

```bash
# Run all checks (auto-detect workspace from cwd)
bakar doctor

# Run checks for a specific BSP
bakar doctor -f imx-6.12.49-2.2.0.xml
bakar doctor my-project.yml

# After a build: also scan the native work tree for glibc leaks
bakar doctor --post-build
```

## Check categories

Checks cover:

- Container runtime (Docker daemon version >= 20.10, storage driver, kas-container image present)
- Host tools (`repo`, `kas-container`, `git`, global git identity)
- Disk space (build root partition, ccache fill ratio)
- CPU threads (resolved `NPROC` and the bitbake settings it drives: task threads, parse threads, `make -j`; flags `local.conf` assignments that override the NPROC-derived values)
- Workspace filesystem (rejects vfat/exfat/ntfs/9p/nfs; sstate hardlinks need a local fs)
- NFS delegations on the build device (warns when this host exports the build tree and a peer holds delegations on it: every conflicting local open must recall one, blocking in the kernel's `__break_lease` for up to `/proc/sys/fs/lease-break-time` seconds each, which makes a local build look hung - ~0% CPU cooker, no parser children, no progress)
- Kernel sysctls (`fs.inotify.max_user_instances`, `fs.inotify.max_user_watches`)
- Kas YAML syntax (`kas dump` parse check)
- BSP-specific checks (repo manifest validity for NXP)
- PSI pressure support (kernel feature check, threshold calibration)
- Persistent hashserv daemon (when `[build] hashserv = true` - PID + TCP probe; see [hashserv.md](hashserv.md))
- sstate hash leak (host-specific variables that corrupt sstate task signatures)
- Uninative wiring (when `[build] uninative = true` - the host tarball's fragment, its glibc ceiling, its payload integrity, its `DL_DIR` cache state, and its consistency across a cluster)

## sstate hash-leak check

The `sstate-hash-leak` check scans `build/conf/local.conf` (plus sibling
conf-includes and active overlays) for assignments of host-specific variables
that leak into bitbake task signatures and break sstate reuse across builds and
hosts.

Variables scanned:

| Variable | Why it leaks |
|----------|--------------|
| `DATETIME` | changes on every build |
| `BUILD_REPRODUCIBLE_BINARIES` | host-dependent reproducibility flag |
| `PWD` | absolute build path varies per checkout |
| `USER` | varies per developer |
| `HOME` | varies per developer |
| `HOSTNAME` | varies per machine |

Severity is **WARN**, never BLOCK. It scans config text, not real signatures,
so it advises rather than stopping a build. The check reads host-side files, so
it runs in host mode too. It is skipped (no finding) when
`build/conf/local.conf` does not exist yet (pre-sync).

For each variable assigned without a matching `[vardepsexclude]` annotation, the
finding's fix hint contains the exact remediation line:

```text
DATETIME[vardepsexclude] += "DATETIME"
```

Add that annotation in `local.conf` (or an overlay) so the variable does not
corrupt sstate hashes.

## Uninative wiring checks

Uninative is oe-core's prebuilt glibc plus dynamic loader that every `-native`
binary is relocated against, so native artifacts stay reusable across hosts with
different system glibcs. The `yocto-uninative-tarball` Arch package replaces the
upstream tarball with one built from the host's own glibc and installs a bitbake
fragment at `/usr/share/yocto-uninative/uninative.inc` that raises
`UNINATIVE_MAXGLIBCVERSION` to match. `[build] uninative = true` makes bakar
append an overlay that pulls that fragment in. These seven checks verify the
wiring took effect and that the raised ceiling is safe.

| Check | Severity | Asserts | Fix hint |
|-------|----------|---------|----------|
| `uninative-fragment` | BLOCK | The fragment is installed and parses, when `[build] uninative` is on | Install `yocto-uninative-tarball`, or set `[build] uninative = false` |
| `uninative-glibc` | BLOCK | The fragment's `UNINATIVE_MAXGLIBCVERSION` is at least the buildtools sysroot's glibc | Rebuild the tarball against a glibc at least as new as the sysroot's, or pin older buildtools |
| `uninative-checksum` | BLOCK | The fragment's declared `UNINATIVE_CHECKSUM[x86_64]` equals the SHA-256 of the payload in its own mirror | Reinstall or rebuild `yocto-uninative-tarball` so fragment and payload agree |
| `uninative-dldir-links` | BLOCK | No cache entry under `<DL_DIR>/uninative` holds a dangling payload link | Repairs itself by removing the entry, which clears its `.done` stamp; re-run the build to refetch |
| `uninative-mirror-hit` | WARN | The cached payload is a symlink into the fragment's own mirror rather than a network fetch | Confirm the mirror holds the payload and that `PREMIRRORS` is not overriding it |
| `uninative-cluster-ceiling` | BLOCK | Every node on the shared mount resolved the same ceiling | Install the same `yocto-uninative-tarball` build on every node, then re-run doctor on each |
| `uninative-leak` | BLOCK | No native artifact in the finished build's work tree reaches a glibc version node above the ceiling | `bitbake -c cleansstate` the named recipes, then find why the compile escaped the buildtools toolchain |

### Why `uninative-fragment` blocks

The overlay that wires uninative in is selected only when the fragment file
exists. A host without the package therefore drops the whole wiring without a
word: no parse error, no warning, just a build that quietly falls back to
oe-core's own `UNINATIVE_MAXGLIBCVERSION` cap. Asking for the feature and
silently not getting it is the failure mode this check exists to make loud, so
it blocks rather than warns.

### Why `uninative-glibc` compares against the buildtools sysroot

The number that matters is the buildtools sysroot's glibc, not the host's.
Every native binary is linked by the pinned buildtools toolchain, so the highest
`GLIBC_x.y` version node those binaries can emit is bounded by the sysroot's
glibc - the host's own glibc never enters the link. If the tarball's ceiling
sits below that, native binaries reference version nodes the uninative loader
cannot resolve, and the artifacts fail to load. An exact match passes but leaves
no headroom: the next buildtools bump breaks the invariant unless the tarball is
rebuilt in the same step.

### `uninative-leak` and `--post-build`

`bakar doctor --post-build` adds the post-build checks to the ordinary pre-flight
run - it does not replace them. Results flow through the same `--json` shape and
the same exit-2 rule for BLOCK failures. The flag is off by default because the
scan walks the whole native work tree, and a pre-flight run has nothing built for
it to read.

`uninative-leak` is the only check that tests the override against real output
rather than asserting it. It walks the native work tree and compares every glibc
version node it can reach against the fragment's ceiling. Reaching covers two
paths: the artifact's own version nodes, and nodes one `DT_NEEDED` edge away
through a host library that `ASSUME_PROVIDED`/`HOSTTOOLS` let a configure script
pick up. The second path is the one that bites, because such an artifact's own
symbol table reads clean while the library it pulls in was built against the host
glibc.

> **Known limitation.** The dependency half of this comparison currently reads
> the versions a host library *defines* rather than the versions the artifact
> *requires* from it, so on a host whose libc defines nodes above the ceiling it
> reports most recipes as leaking. Treat a `uninative-leak` BLOCK as a prompt to
> investigate, not as proof of a leak, until that is corrected. The six
> pre-flight uninative checks above are unaffected.

Two lesser outcomes are not a clean bill of health. When `objdump` is absent the
check skips and says so - unscanned, not clean. When some dependency cannot be
resolved it reports WARN naming it, because an unchecked dependency is not
evidence of a clean tree. The leak scan has not yet been validated against a real
image build.

### When these checks do not run

All seven need `[build] uninative = true` (default off), host mode, and an
Arch-family host; otherwise every one of them reports INFO with the reason.
`uninative-cluster-ceiling` additionally needs `[build] cluster = true`, and
`uninative-leak` runs only under `--post-build` against a completed build.

## PSI calibration

Set `psi_autocalibrate = true` under `[build]` in
`~/.config/bakar/config.toml`. `bakar build` then samples `/proc/pressure`
during every build and writes the recommended `pressure_max_*` back to the
config afterwards, reporting what changed. The first build bootstraps the
values; later builds only raise a threshold (from an unthrottled
measurement), never lower it, so a light sstate-cached build cannot
over-throttle the next cold one. Delete the `pressure_max_*` keys to
recalibrate from scratch. See [configuration.md](configuration.md).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed (or only WARN/INFO findings) |
| 2 | At least one BLOCK-severity check failed |

## See also

- [build.md](build.md) - doctor runs automatically before every build
- [configuration.md](configuration.md) - `build.show_doctor_report` flag and the global `--hide-doctor-report` option to hide the report
- [hashserv.md](hashserv.md) - what `check_hashserv` actually probes and how to fix its findings

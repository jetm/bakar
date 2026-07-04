#
# sccache.bbclass - route eligible target compiles through sccache-dist.
#
# Modeled on oe-core's meta/classes/ccache.bbclass: set CCACHE per-recipe
# through an anonymous python gate so only selected recipes route through the
# launcher, and honor the same per-recipe CCACHE_DISABLE escape hatch. This
# class distributes on an ALLOW-LIST: only recipes named in SCCACHE_INCLUDED_PN
# set CCACHE, so only they reach the sccache client daemon at all. Every other
# recipe compiles plain-local and never contacts the daemon.
#
# Why an allow-list, not a deny-list: sccache-dist ships compile-units to a
# remote build server, but every compile - distributable or not - must first
# visit the single client daemon, which preprocesses locally (cc1 -E) and
# packages inputs. On a whole image that counter is the throughput ceiling:
# thousands of cheap objects plus autoconf's conftest storm queue behind it and
# starve make's job slots, so distribution loses to plain ccache. The win lives
# only in heavy-object recipes (llvm-native measured 103x of -j53, the
# cross-toolchain C++ builds) where a ~9 CPU-s object dwarfs the per-compile
# tax. So distribute ONLY those and let bitbake+sstate carry the rest.
#
# CCACHE is set globally within an allow-listed recipe (NOT task-scoped): OE
# bakes ${CCACHE} into CC at do_configure and oe_runmake at do_compile passes no
# CC= override, so a task-scoped CCACHE would leave configure baking bare gcc
# and nothing would distribute. Global CCACHE means an allow-listed recipe's own
# conftests also invoke sccache, but sccache tags conftests "not eligible for
# distributed compilation" and keeps them local, and only a handful of recipes
# carry the launcher, so the conftest load on the daemon is bounded. Enable with
# `INHERIT += "sccache"` (the bakar sccache tuning overlay does this).
#
# sccache-dist differs from ccache in three ways that this class must handle,
# none of which ccache.bbclass needs to:
#
#  1. It ships jobs to a remote build-server, so it must stay OFF the build/host
#     compiler. OE prepends ${CCACHE} to BUILD_CC/BUILD_CXX (gcc-native.bbclass)
#     as well as the target CC. A leaked sccache there ships host-tool compiles
#     (e.g. linux-libc-headers' fixdep, built with HOSTCC="${BUILD_CC}") to the
#     build-server, where they need network the install task lacks and hit the
#     unpackageable host `as`. Force BUILD_CC/BUILD_CXX back to the
#     gcc-native.bbclass definitions minus ${CCACHE}; :forcevariable beats the
#     class assignment regardless of inherit order.
#  2. It cannot package a host gcc whose assembler is PATH-relative (Arch's host
#     gcc reports a bare `as`), so native, cross, and crosssdk recipes - whose CC
#     IS the host compiler - must compile locally. The class gate excludes them.
#     nativesdk and cross-canadian recipes are NOT excluded: they build with the
#     OE crosssdk compiler (absolute-path `as`, packageable), so they distribute
#     like any target cross-compile.
#  3. The kernel distributes like any other target recipe. A few of its objects
#     .incbin a binary the inputs packager cannot ship (the vdso, embedded
#     config, and dtb wrappers) and fail to assemble remotely; sccache falls
#     back to a local recompile for those, so the kernel needs no wholesale
#     exclusion (1124 of 1128 kernel compiles distribute).

# Per-recipe opt-out, mirrors CCACHE_DISABLE.
SCCACHE_DISABLE ??= ""

# Allow-list: ONLY these recipes distribute; everything else compiles
# plain-local and never contacts the daemon. Membership is the measured (or
# strongly-inferred) set of heavy-object recipes where a distributed compile's
# per-object tax (local cc1 -E + round trip + input packaging) is dwarfed by the
# object's own cost, so distribution pays 2-3x. Grouped by tier:
#
# Toolchain / LLVM (built on most non-trivial builds, expensive C++):
#   llvm-native                   - 2571 C++ objects ~9 CPU-s each, measured 103x
#                                   of -j53 (23170 CPU-s into 224s wall). The one
#                                   hard-measured win. It distributes via
#                                   OECMAKE_*_COMPILER_LAUNCHER (set below)
#                                   regardless of CCACHE, but is listed so the
#                                   gate reaches that launcher setup.
#   gcc-cross-${TARGET_ARCH}      - the cross C++ compiler build, critical path.
#   binutils-cross-${TARGET_ARCH} - measured 306 compiles distributed, 0-error.
#   gcc-runtime, gcc-sanitizers   - libstdc++/libgcc and asan/tsan/ubsan runtimes,
#                                   large C++ (a few objects fall back local on a
#                                   -Wimplicit-fallthrough/soft-float preprocessing
#                                   mismatch, harmless).
#   clang, clang-cross-${TARGET_ARCH}, clang-crosssdk-${SDK_ARCH}
#                                 - target and cross Clang, same heavy C++ profile.
#   compiler-rt, libcxx, openmp   - LLVM runtime C++ built with the clang toolchain.
#   rust-llvm, rust-llvm-native   - the LLVM (C++) behind rustc; the rustc crates
#                                   themselves are NOT distributable (no sccache
#                                   Rust backend) but this LLVM build is.
#
# Kernel and init - DELISTED. linux-yocto and systemd were on the list for the
# first instrumented cold run; they distribute cleanly but by the object-cost
# dividing line below they are qemu-shaped, not llvm-shaped (kernel C objects
# ~0.2-0.5 CPU-s, systemd's C similar). "1124 of 1128 kernel compiles distribute"
# proves they CAN, not that they PAY, and the first cold core-image-minimal run
# landed at ~parity with the plain-ccache baseline, so distributing this cheap
# tail buys nothing on minimal. They now compile locally via the ccache tail. Re-
# add either only if a per-recipe do_compile A/B (distributed vs plain buildstats)
# later shows it pays.
#
# Feed/extra big C++ (only built for the larger rootfs with `opengl wayland`; an
# inert no-op on core-image-minimal, where none of them are scheduled):
#   chromium-ozone-wayland, chromium-x11 - full Chromium 146 C++ tree.
#   qtwebengine, wpewebkit        - embedded browser engines, huge C++ TUs.
#   qtbase, qtdeclarative         - large C++ frameworks.
#   opencv                        - C++ vision library, template/SIMD-heavy TUs.
#
# nodejs is deliberately NOT allow-listed even though it bundles V8 (template-
# heavy C++). Under the hybrid the ccache overlay is co-selected, and it carries
# CCACHE_DISABLE:pn-nodejs = "1" (nodejs's GYP Makefile writes a .d.raw dep file
# via -MF then reads it back, which ccache with hash_dir=false does not restore
# on a hit). sccache.bbclass honors CCACHE_DISABLE (the gate early-returns), so
# distributing nodejs would need it OFF the ccache-disable list - but whether
# sccache restores the .d.raw file is untested, so nodejs stays local-uncached
# pending that test rather than risking an unverified GYP break.
#
# A listed recipe that is not built is inert. The cross/native recipes compile
# with the host/build compiler, whose `-print-prog-name=as` returns a bare `as`;
# the sccache fork resolves it against the compile task's PATH (icecc.bbclass's
# which(PATH) fallback) rather than the daemon's PATH, so the right assembler is
# packaged. Any dist-infra failure falls back to a correct local recompile, so a
# listed recipe never breaks - a bad fit only wastes round-trips. The dividing
# line is object cost, not recipe identity: qemu-system-native (~1.0 CPU-s per
# object over 5516 ninja objects, ~2x SLOWER distributed than local -j53) is the
# archetypal loser and stays OFF the list. The glibc bootstrap family (glibc,
# glibc-initial, libgcc, libgcc-initial) is DELIBERATELY omitted for the same
# reason: it distributed heavily under the old deny-list (glibc alone 6427/6429
# objects) but its objects are cheap-to-moderate C, so it is qemu-shaped, not
# llvm-shaped - it now gets local ccache via the hybrid tail instead. Multilib
# variants (lib32-gcc-runtime, etc.) do not match the bare PNs and also run
# local via ccache; that is intended.
SCCACHE_INCLUDED_PN ?= "llvm-native gcc-cross-${TARGET_ARCH} binutils-cross-${TARGET_ARCH} gcc-runtime gcc-sanitizers clang clang-cross-${TARGET_ARCH} clang-crosssdk-${SDK_SYS} compiler-rt libcxx openmp rust-llvm rust-llvm-native chromium-ozone-wayland chromium-x11 qtwebengine wpewebkit qtbase qtdeclarative opencv"

python () {
    if (bb.utils.to_boolean(d.getVar('SCCACHE_DISABLE')) or
            bb.utils.to_boolean(d.getVar('CCACHE_DISABLE'))):
        return
    if d.getVar('PN') not in (d.getVar('SCCACHE_INCLUDED_PN') or '').split():
        return
    # Launch the compiler through sccache globally, exactly as oe-core's
    # ccache.bbclass does (`CCACHE = 'ccache '`). OE bakes ${CCACHE} into CC
    # (`CC = "${CCACHE}${HOST_PREFIX}gcc ..."`), so with a global CCACHE autotools'
    # do_configure captures `CC = sccache gcc` into the generated Makefile and
    # oe_runmake at do_compile (which passes no CC= override) still invokes
    # sccache. Scoping CCACHE to do_compile only (the earlier approach) left
    # configure baking bare gcc, so make ran plain gcc and nothing distributed.
    # sccache already keeps configure's conftests local on its own - it tags them
    # "not eligible for distributed compilation" - so a global launcher does not
    # flood the cluster with conftest round-trips.
    d.setVar('CCACHE', 'sccache ')
    # cmake.bbclass's oecmake_map_compiler strips only 'ccache' from CC, never
    # 'sccache', so with the global CCACHE it reads CC="sccache gcc" as
    # compiler=sccache; the explicit launcher below would then double it into
    # `sccache sccache ...`, and the inner sccache dies on the preprocessor's -E
    # ("unexpected argument '-E'") - breaking every CMake recipe's compiler check.
    # Mirror the ccache handling for sccache: point the CMake compiler at the real
    # gcc/g++ (the word after the launcher) and keep sccache as the launcher.
    # autotools (bakes CC) and meson (uses the whole CC array) need no such fixup.
    for cvar, srcvar in (('OECMAKE_C_COMPILER', 'CC'), ('OECMAKE_CXX_COMPILER', 'CXX')):
        words = (d.getVar(srcvar) or '').split()
        if len(words) > 1 and words[0] == 'sccache':
            d.setVar(cvar, words[1])
    d.setVar('OECMAKE_C_COMPILER_LAUNCHER', 'sccache')
    d.setVar('OECMAKE_CXX_COMPILER_LAUNCHER', 'sccache')

    # Route rustc through sccache for cargo recipes so Rust compiles cache and
    # distribute like C/C++. cargo honors RUSTC_WRAPPER natively - but it must
    # NOT be the bare `sccache`. cc-rs (the `cc` crate that -sys build scripts
    # use to compile C) checks RUSTC_WRAPPER's file stem against
    # {"sccache", "cachepot"} (cc rustc_wrapper_fallback) and, on a match, ALSO
    # prepends it to the C compiler. In an OE rust build that C compiler is the
    # `target-rust-cc` wrapper script, which sccache cannot identify ("Compiler
    # not supported"), so a bare RUSTC_WRAPPER=sccache breaks every cargo recipe
    # carrying a cc-rs C dependency (rust-native via lzma-sys, avocadoctl via
    # aws-lc-sys). Point RUSTC_WRAPPER at a differently-named shim that just
    # execs sccache: its stem is not in cc-rs's list, so cargo still
    # caches/distributes rustc while cc-rs leaves C on the normal (already
    # sccache-routed) compiler path. The shim is written by the prefunc below,
    # on both do_compile and do_install because rust-native compiles rustc in
    # do_install (bootstrap.py) while ordinary cargo recipes compile in
    # do_compile - and a prefunc runs whenever its task actually runs, so it
    # survives an sstate-restored sibling task.
    if bb.data.inherits_class('cargo_common', d):
        d.setVar('RUSTC_WRAPPER', d.getVar('WORKDIR') + '/sccache-rustc-shim/rustc-cache')
        d.setVarFlag('RUSTC_WRAPPER', 'export', '1')
        d.appendVarFlag('do_compile', 'prefuncs', ' sccache_write_rustc_shim')
        d.appendVarFlag('do_install', 'prefuncs', ' sccache_write_rustc_shim')
}

# Write the rustc shim (see the RUSTC_WRAPPER note in the gate above). cc-rs
# keys off the wrapper's file stem, so the shim must be named something other
# than `sccache`/`cachepot`; it just execs the real sccache, which is on the
# task PATH via HOSTTOOLS. Idempotent, so running it from two prefuncs is fine.
sccache_write_rustc_shim () {
    mkdir -p ${WORKDIR}/sccache-rustc-shim
    printf '#!/bin/sh\nexec sccache "$@"\n' > ${WORKDIR}/sccache-rustc-shim/rustc-cache
    chmod +x ${WORKDIR}/sccache-rustc-shim/rustc-cache
}

# Route the build/host compiler through sccache too (${CCACHE} restored).
# Non-allow-listed recipes never set CCACHE, so this expands to a bare compiler
# and stays local; allow-listed recipes get "sccache <gcc>" and distribute now
# that the fork resolves the bare `as` against the compile PATH. Definitions
# mirror gcc-native.bbclass.
BUILD_CC:forcevariable = "${CCACHE}${BUILD_PREFIX}gcc ${BUILD_CC_ARCH}"
BUILD_CXX:forcevariable = "${CCACHE}${BUILD_PREFIX}g++ ${BUILD_CC_ARCH}"

# Put sccache on bitbake's task PATH. OE restricts each task's PATH to sysroot
# bins plus the HOSTTOOLS allowlist (tmp/hosttools/); the host /usr/bin/sccache
# is invisible to recipes unless allowlisted.
HOSTTOOLS += "sccache"

# Let the compiler reach the scheduler. bitbake runs each task in a fresh
# network namespace (loopback down) via unshare(CLONE_NEWNET) unless the task
# sets [network] = "1" - only do_fetch opts in by default. CCACHE is set
# globally for an allow-listed recipe, so its sccache client runs in every
# compile-bearing task; but only do_compile (and its ptest mirror) DISPATCHES to
# the cluster. do_configure's conftests are tagged not-eligible and kept local
# by sccache, so configure/install reach no scheduler and need no network grant;
# granting them one would be dead config. This conclusion depends on the daemon
# being pre-spawned OUTSIDE any task netns: with global CCACHE the first sccache
# call of a listed recipe is usually a do_configure conftest, and if no daemon
# exists yet the client would auto-spawn one INSIDE configure's network-less
# namespace - a daemon that can never reach the scheduler and poisons every later
# dispatch. What prevents that today is the sccache_dist_guard BuildStarted
# handler below, which runs --dist-status + a probe compile in the cooker's
# networked environment, so a connected daemon always exists before any
# task-netns conftest runs (host mode also pre-starts it). If that guard is ever
# removed or conditionalized, this narrative inverts - keep them together.
do_compile[network] = "1"
do_compile_ptest_base[network] = "1"
# linux-yocto defines two extra compile tasks beyond do_compile that run
# CC="sccache <gcc>" through oe_runmake: do_compile_kernelmodules and
# do_bundle_initramfs (via kernel_do_compile, only when bundling an initramfs).
# They carry CCACHE:task-compile-kernelmodules / CCACHE:task-bundle-initramfs, so
# they distribute and need network. The kconfig-probe tasks (do_kernel_configme
# via `make alldefconfig`, do_kernel_configcheck via symbol_why.py) compile
# locally with plain gcc - they are outside the compile-task scope - so they
# reach no scheduler and need no network grant.
do_compile_kernelmodules[network] = "1"
do_bundle_initramfs[network] = "1"

# Point the in-build sccache client at the configured scheduler. Container mode
# bakes a literal `export SCCACHE_DIST_SCHEDULER_URL = "<url>"` into local.conf
# (bakar _inject_literal_sccache) because kas's clean_environment drops the
# BAKAR_* env this class would otherwise read - the same scrubbing that defeats
# the BB_ENV_PASSTHROUGH_ADDITIONS whitelist. local.conf is parsed before
# `INHERIT += "sccache"` pulls this class in, so a plain `=` here, parsed last,
# would clobber that materialized literal back to the empty env lookup. Use a
# weak default (??=) so the local.conf `=` always wins, with a bare `export` to
# carry the export flag onto whichever value survives. Host mode injects nothing
# and leaves this empty, which is correct: the pre-started daemon reads the
# scheduler from ~/.config/sccache/config.
export SCCACHE_DIST_SCHEDULER_URL
SCCACHE_DIST_SCHEDULER_URL ??= "${@os.environ.get('BAKAR_SCCACHE_SCHEDULER_URL', '')}"

# Container mode only: deliver the auth config path and a writable disk cache to
# the in-container client (bakar sets BAKAR_SCCACHE_CONF/BAKAR_SCCACHE_DIR there;
# host mode leaves them unset, where the pre-started server already reads
# ~/.config/sccache/config and the configured cache dir). Export only when set -
# an empty SCCACHE_CONF would point the client at "" and lose the host-mode auth
# token, so this must not mirror the always-exported scheduler line above.
python () {
    for envname, taskvar in (('BAKAR_SCCACHE_CONF', 'SCCACHE_CONF'),
                             ('BAKAR_SCCACHE_DIR', 'SCCACHE_DIR')):
        value = os.environ.get(envname)
        if value:
            d.setVar(taskvar, value)
            d.setVarFlag(taskvar, 'export', '1')
}

# Build-end diagnostic: print one aggregate per-server distribution summary.
# sccache schedules per compile job, not per recipe, so per-recipe->node
# attribution is not well-defined; the honest build-end view is per-server
# counts. --show-stats reports the client daemon's counters cumulatively since
# the last --zero-stats, and in host mode the daemon persists across builds, so
# zero at BuildStarted to scope the summary to this build. Silent unless dist is
# enabled (SCCACHE_DIST_SCHEDULER_URL set) and the daemon reports activity.
python sccache_dist_summary () {
    import bb.event
    import json
    import shutil
    import subprocess

    if not d.getVar('SCCACHE_DIST_SCHEDULER_URL'):
        return
    sccache = shutil.which('sccache')
    if not sccache:
        return

    # Query the same sccache server the compile tasks use. In container mode the
    # tasks get SCCACHE_CONF/SCCACHE_DIR from the per-task python block above
    # (mapped from the container-injected BAKAR_* vars); this handler runs in the
    # cooker, whose environment carries the BAKAR_* vars but not the SCCACHE_*
    # ones. Without the same mapping the cooker's sccache targets the default
    # cache dir - absent and unwritable in the container - so --zero-stats never
    # starts a server and --show-stats reports zero, silently dropping the
    # summary. Host mode leaves BAKAR_* unset, so the environment is unchanged
    # there and the pre-started host server is queried as before.
    env = dict(os.environ)
    for envname, sccname in (('BAKAR_SCCACHE_CONF', 'SCCACHE_CONF'),
                             ('BAKAR_SCCACHE_DIR', 'SCCACHE_DIR')):
        value = os.environ.get(envname)
        if value:
            env[sccname] = value

    if isinstance(e, bb.event.BuildStarted):
        subprocess.run([sccache, '--zero-stats'], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if not isinstance(e, bb.event.BuildCompleted):
        return

    try:
        proc = subprocess.run([sccache, '--show-stats', '--stats-format=json'],
                              env=env, capture_output=True, text=True, timeout=15)
        stats = json.loads(proc.stdout)['stats']
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return

    dist = stats.get('dist_compiles', {})
    distributed = sum(dist.values())
    fell_back = stats.get('dist_errors', 0)
    if not distributed and not fell_back:
        return

    hits = sum(stats.get('cache_hits', {}).get('counts', {}).values())
    misses = sum(stats.get('cache_misses', {}).get('counts', {}).values())
    hit_rate = 100.0 * hits / (hits + misses) if hits + misses else 0.0

    per_node = ', '.join('%s %d' % (addr, n) for addr, n in sorted(dist.items()))
    bb.plain('sccache-dist: %d distributed (%s), %d fell back to local, cache %.1f%% hit'
             % (distributed, per_node or 'none', fell_back, hit_rate))
}
addhandler sccache_dist_summary
sccache_dist_summary[eventmask] = "bb.event.BuildStarted bb.event.BuildCompleted"

# Build-start guard: when distributed compilation was requested
# (SCCACHE_DIST_SCHEDULER_URL set), fail fast on any wiring that would silently
# degrade the whole image to LOCAL-ONLY while the cluster sits idle, wasting
# hours before anyone notices. Three gates, cheapest first:
#
#  1. Config: the client reads its scheduler URL and bearer token only from
#     SCCACHE_CONF (no env override for either). Assert the config the daemon
#     will use carries a [dist] scheduler_url and a non-empty [dist.auth] token.
#  2. Reachability: `sccache --dist-status` must report >= 1 build server.
#  3. Auth (end-to-end): --dist-status hits the scheduler's UNAUTHENTICATED
#     /api/v1/scheduler/status, so gates 1-2 cannot prove the token is accepted
#     for job allocation. The token-gated /api/v1/scheduler/alloc_job 401s a bad
#     token and the client falls back to local, undetected. So distribute one
#     throwaway compile and confirm it actually reached the cluster.
#
# Reads SCCACHE_CONF from the datastore (NOT os.environ, which clean_environment
# scrubs - the exact failure this guards against) and maps it into the query env
# like the summary handler does.
python sccache_dist_guard () {
    import bb.event
    import json
    import shutil
    import subprocess
    import tempfile

    if not isinstance(e, bb.event.BuildStarted):
        return
    if not d.getVar('SCCACHE_DIST_SCHEDULER_URL'):
        return
    sccache = shutil.which('sccache')
    if not sccache:
        bb.fatal('sccache-dist requested but the sccache binary is not on PATH in the build environment')

    conf_path = d.getVar('SCCACHE_CONF') or os.environ.get('BAKAR_SCCACHE_CONF')
    env = dict(os.environ)
    for sccname in ('SCCACHE_CONF', 'SCCACHE_DIR'):
        value = d.getVar(sccname) or os.environ.get('BAKAR_' + sccname)
        if value:
            env[sccname] = value

    # Gate 1 (config): only fatal when the config parsed and the token/url is
    # genuinely absent. A tomllib import or parse failure skips this gate (gate 3
    # still catches a broken token end-to-end) rather than false-blocking a build.
    if conf_path and os.path.isfile(conf_path):
        conf = None
        try:
            import tomllib
            with open(conf_path, 'rb') as cf:
                conf = tomllib.load(cf)
        except (OSError, ValueError, ImportError):
            conf = None
        if isinstance(conf, dict):
            dist_conf = conf.get('dist', {}) or {}
            token = (dist_conf.get('auth', {}) or {}).get('token')
            if not dist_conf.get('scheduler_url') or not token:
                bb.fatal(
                    'sccache-dist was requested but %s is missing a [dist] scheduler_url or a '
                    '[dist.auth] token. The unauthenticated scheduler status probe would still '
                    'pass, but token-gated job allocation (/api/v1/scheduler/alloc_job) would '
                    '401 and every compile would run LOCAL-ONLY.' % conf_path)

    # Gate 2 (reachability): scheduler up with >= 1 build server.
    num_servers = 0
    detail = ''
    try:
        proc = subprocess.run([sccache, '--dist-status'], env=env,
                              capture_output=True, text=True, timeout=30)
        detail = (proc.stdout or proc.stderr).strip()
        sched = json.loads(proc.stdout).get('SchedulerStatus')
        if isinstance(sched, list) and len(sched) > 1:
            num_servers = sched[1].get('num_servers', 0)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as ex:
        detail = detail or str(ex)

    if num_servers < 1:
        bb.fatal(
            'sccache-dist was requested (--sccache-dist) but the in-container client '
            'is not reaching the scheduler, so every compile would run LOCAL-ONLY and '
            'the cluster would sit idle.\n'
            '  SCCACHE_CONF=%s\n'
            '  SCCACHE_DIST_SCHEDULER_URL=%s\n'
            '  sccache --dist-status: %s\n'
            'Container builds need SCCACHE_CONF/SCCACHE_DIR materialized into local.conf '
            '(bakar _inject_literal_sccache); host builds need the pre-started daemon '
            'configured with the dist config.'
            % (d.getVar('SCCACHE_CONF') or '(unset)',
               d.getVar('SCCACHE_DIST_SCHEDULER_URL') or '(unset)',
               detail or '(no output)'))

    # Gate 3 (auth, end-to-end): distribute one throwaway compile and confirm it
    # reached the cluster. A bare gcc is the build/host compiler the fork
    # distributes via PATH-resolved `as`; absent it, skip rather than block. Zero
    # stats around the probe so it is measured in isolation and does not pollute
    # the build-end summary (this handler runs after the summary's BuildStarted
    # --zero-stats, so the re-zero leaves the build's own counters clean).
    gcc = shutil.which('gcc')
    if not gcc:
        bb.warn('sccache-dist guard: no gcc found to run the auth probe compile; '
                'skipping the end-to-end auth check (reachability only)')
        bb.plain('sccache-dist: scheduler reachable, %d build server(s) - distribution active' % num_servers)
        return

    subprocess.run([sccache, '--zero-stats'], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe_rc = 1
    probe_err = ''
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 'sccache_dist_probe.c')
        obj = os.path.join(td, 'sccache_dist_probe.o')
        # Unique body -> guaranteed cache miss -> real compile -> dispatch.
        with open(src, 'w') as sf:
            sf.write('int sccache_dist_probe_%d(void) { return 0; }\n' % os.getpid())
        try:
            probe = subprocess.run([sccache, gcc, '-c', src, '-o', obj], env=env,
                                   capture_output=True, text=True, timeout=120)
            probe_rc = probe.returncode
            probe_err = (probe.stderr or '').strip()
        except (OSError, subprocess.SubprocessError) as ex:
            probe_err = str(ex)

    distributed = 0
    dist_errors = 0
    try:
        st = json.loads(subprocess.run(
            [sccache, '--show-stats', '--stats-format=json'],
            env=env, capture_output=True, text=True, timeout=15).stdout)['stats']
        distributed = sum(st.get('dist_compiles', {}).values())
        dist_errors = st.get('dist_errors', 0)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    subprocess.run([sccache, '--zero-stats'], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if probe_rc != 0:
        bb.warn('sccache-dist guard: the auth probe compile did not run cleanly (%s); '
                'skipping the end-to-end auth check (reachability only)'
                % (probe_err or 'no output'))
    elif distributed < 1:
        bb.fatal(
            'sccache-dist auth probe FELL BACK to local (%d dist error(s)): the scheduler is '
            'reachable but the client could not allocate a job, so the token-gated '
            '/api/v1/scheduler/alloc_job rejected the bearer token (401) or the dispatch '
            'failed. Every compile would run LOCAL-ONLY. Verify the [dist.auth] token in %s '
            'matches the scheduler; re-run if the cluster was momentarily busy.'
            % (dist_errors, conf_path or '(unset)'))

    bb.plain('sccache-dist: scheduler reachable, %d build server(s), dispatch authenticated - distribution active'
             % num_servers)
}
addhandler sccache_dist_guard
sccache_dist_guard[eventmask] = "bb.event.BuildStarted"

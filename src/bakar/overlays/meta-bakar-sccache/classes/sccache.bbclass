#
# sccache.bbclass - route eligible target compiles through sccache-dist.
#
# Modeled on oe-core's meta/classes/ccache.bbclass: set CCACHE per-recipe
# through an anonymous python gate so only compatible recipes route through the
# launcher, and honor the same per-recipe CCACHE_DISABLE escape hatch. Enable
# with `INHERIT += "sccache"` (the bakar sccache tuning overlay does this and
# also `INHERIT:remove = "ccache"`, since the two launchers are mutually
# exclusive).
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

# No classes excluded: native, cross, and crosssdk all distribute now. They
# compile with the host/build compiler, whose `-print-prog-name=as` returns a
# bare `as`; the sccache fork resolves that against the compile task's PATH (the
# same -print-prog-name then which(PATH) fallback OE's icecc.bbclass used) rather
# than the daemon's PATH, so the right assembler is packaged. Verified on avocado
# scarthgap against the fixed cluster: zlib-native and linux-libc-headers (711
# tasks) for native, and binutils-cross-aarch64 (306 compiles distributed to the
# second node) for cross - all 0-error. crosssdk shares that identical
# host-compiler/bare-`as` path (only the target triple differs, which does not
# affect host-side `as` packaging) and is exercised only during SDK builds.
# nativesdk and cross-canadian were never excluded (OE crosssdk compiler,
# absolute paths, already packageable).
SCCACHE_EXCLUDED_CLASSES ?= ""

# Target recipes that must compile locally even though their class is eligible.
# Empty: the gcc/glibc bootstrap recipes (glibc, glibc-initial, libgcc,
# libgcc-initial, gcc-runtime, gcc-sanitizers) used to be listed here, but they
# distribute cleanly now that the sccache-dist client falls back to a local
# recompile on any dist-infra failure. The fallback covers the two failure points
# these recipes hit: glibc's per-object `.o.dt` dependency file, which the server
# drops from the returned output set, and the libgcc/gcc-sanitizers soft-float
# files whose -Wimplicit-fallthrough suppressing comments preprocessing strips, so
# the remote -Werror compile errors where a local one (comments intact) does not.
# Both fall back and the local recompile succeeds; the vast majority of objects in
# these recipes still distribute (glibc 6427/6429, libgcc 604/608, gcc-runtime
# 1026/1026, gcc-sanitizers ~960/1066). Add a PN here to force a recipe local when
# its dist round-trips never pay off.
SCCACHE_EXCLUDED_PN ?= ""

python () {
    if (bb.utils.to_boolean(d.getVar('SCCACHE_DISABLE')) or
            bb.utils.to_boolean(d.getVar('CCACHE_DISABLE'))):
        return
    if d.getVar('PN') in d.getVar('SCCACHE_EXCLUDED_PN').split():
        return
    for cls in d.getVar('SCCACHE_EXCLUDED_CLASSES').split():
        if bb.data.inherits_class(cls, d):
            return
    d.setVar('CCACHE', 'sccache ')
}

# Route the build/host compiler through sccache too (${CCACHE} restored).
# Excluded classes never set CCACHE, so this expands to a bare compiler and stays
# local; eligible recipes (e.g. native) get "sccache <gcc>" and distribute now
# that the fork resolves the bare `as` against the compile PATH. Definitions
# mirror gcc-native.bbclass.
BUILD_CC:forcevariable = "${CCACHE}${BUILD_PREFIX}gcc ${BUILD_CC_ARCH}"
BUILD_CXX:forcevariable = "${CCACHE}${BUILD_PREFIX}g++ ${BUILD_CC_ARCH}"

# cmake.bbclass's oecmake_map_compiler splits the compiler launcher out of CC,
# but only recognizes the literal "ccache" - with CC="sccache <gcc>" it makes
# sccache itself the compiler, so cmake's compiler check runs `sccache <flags>`
# and dies "unexpected argument '-m'". Re-derive the OECMAKE compiler/launcher
# split with a helper that recognizes sccache too. cmake.bbclass uses ?= for
# these, so this plain assignment wins; its :allarch = "" override still wins for
# allarch recipes (which do not compile). The NATIVE_* launchers read BUILD_CC,
# already stripped above, so they need no override.
def sccache_map_compiler(varname, d):
    args = (d.getVar(varname) or "").split()
    if args and args[0] in ('ccache', 'sccache'):
        return args[1], args[0]
    return (args[0] if args else ''), ''

OECMAKE_C_COMPILER = "${@sccache_map_compiler('CC', d)[0]}"
OECMAKE_C_COMPILER_LAUNCHER = "${@sccache_map_compiler('CC', d)[1]}"
OECMAKE_CXX_COMPILER = "${@sccache_map_compiler('CXX', d)[0]}"
OECMAKE_CXX_COMPILER_LAUNCHER = "${@sccache_map_compiler('CXX', d)[1]}"

# Put sccache on bitbake's task PATH. OE restricts each task's PATH to sysroot
# bins plus the HOSTTOOLS allowlist (tmp/hosttools/); the host /usr/bin/sccache
# is invisible to recipes unless allowlisted.
HOSTTOOLS += "sccache"

# Let the compiler reach the scheduler. bitbake runs each task in a fresh
# network namespace (loopback down) via unshare(CLONE_NEWNET) unless the task
# sets [network] = "1" - only do_fetch opts in by default. The sccache client
# ships jobs from every task that runs the compiler: do_configure (compiler
# tests), do_compile, do_install (some recipes link with the target gcc at
# install, e.g. glibc's format.lds), and the ptest.bbclass mirrors of all three
# (do_compile_ptest_base builds the test binaries). A [network] flag on a task a
# recipe does not define is harmless.
do_configure[network] = "1"
do_compile[network] = "1"
do_install[network] = "1"
do_configure_ptest_base[network] = "1"
do_compile_ptest_base[network] = "1"
do_install_ptest_base[network] = "1"
# linux-yocto defines extra compiler-bearing tasks beyond do_compile that the
# generic grants above miss. They invoke CC="sccache <gcc>" either directly via
# oe_runmake (do_compile_kernelmodules; do_bundle_initramfs via kernel_do_compile,
# only when bundling an initramfs) or via the kconfig probe in
# scripts/Kconfig.include that every kernel make re-runs (do_kernel_configme via
# `make alldefconfig`, do_kernel_configcheck via symbol_why.py -> kconfiglib).
# Without network the client cannot reach its 127.0.0.1 daemon and the probe
# fails "Network is unreachable" -> "Sorry, this C compiler is not supported".
do_kernel_configme[network] = "1"
do_kernel_configcheck[network] = "1"
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
# (SCCACHE_DIST_SCHEDULER_URL set), fail fast if the in-container client cannot
# reach the scheduler. A broken dist wiring - e.g. SCCACHE_CONF not materialized
# into local.conf, so the daemon starts config-less - otherwise degrades
# silently to local-only: the whole image compiles in-container while the
# cluster sits idle, wasting hours before anyone notices. Reads SCCACHE_CONF
# from the datastore (NOT os.environ, which bitbake's clean_environment scrubs -
# the exact failure this guards against) and maps it into the query env like the
# summary handler does, then asserts the scheduler is reachable with >=1 server.
python sccache_dist_guard () {
    import bb.event
    import json
    import shutil
    import subprocess

    if not isinstance(e, bb.event.BuildStarted):
        return
    if not d.getVar('SCCACHE_DIST_SCHEDULER_URL'):
        return
    sccache = shutil.which('sccache')
    if not sccache:
        bb.fatal('sccache-dist requested but the sccache binary is not on PATH in the build environment')

    env = dict(os.environ)
    for sccname in ('SCCACHE_CONF', 'SCCACHE_DIR'):
        value = d.getVar(sccname) or os.environ.get('BAKAR_' + sccname)
        if value:
            env[sccname] = value

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
    bb.plain('sccache-dist: scheduler reachable, %d build server(s) - distribution active' % num_servers)
}
addhandler sccache_dist_guard
sccache_dist_guard[eventmask] = "bb.event.BuildStarted"

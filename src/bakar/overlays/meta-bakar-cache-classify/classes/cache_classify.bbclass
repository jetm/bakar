#
# cache_classify.bbclass - emit a per-task compile-cache backend classification.
#
# For every recipe (unconditionally - no allow-list, no CCACHE_DISABLE gate),
# attach a prefunc to each compile-bearing task that fires a MetadataEvent
# naming the cache backend in effect (sccache / ccache / none). bakar.eventlog
# joins these rows onto bb.build.TaskStarted rows by exact (_package, _task)
# match to render a per-task cache badge in the build UI; a matching NOTE line
# also reaches kas.log/console.log through bakar's PTY capture.
#
# This class is deliberately unconditional: it observes whatever CCACHE the
# sccache/ccache overlays did (or did not) set, so it must run for recipes those
# overlays skip. It only reads state and emits events - it never sets CCACHE or
# alters the build.

python () {
    d.appendVarFlag('do_compile', 'prefuncs', ' bakar_classify_cache_backend')
    d.appendVarFlag('do_compile_ptest_base', 'prefuncs', ' bakar_classify_cache_backend')
    d.appendVarFlag('do_install', 'prefuncs', ' bakar_classify_cache_backend')
    d.appendVarFlag('do_compile_kernelmodules', 'prefuncs', ' bakar_classify_cache_backend')
    d.appendVarFlag('do_bundle_initramfs', 'prefuncs', ' bakar_classify_cache_backend')
}

python bakar_classify_cache_backend () {
    ccache = d.getVar('CCACHE') or ''
    backend = 'sccache' if 'sccache' in ccache else ('ccache' if 'ccache' in ccache else 'none')

    evt = bb.event.MetadataEvent('bakar-cache-backend', backend)
    # bakar.eventlog._task_key() joins classification rows onto TaskStarted rows
    # by exact (_package, _task) match. bitbake's worker strips the 'do_' prefix
    # from BB_CURRENTTASK (it resolves to e.g. "compile"), but TaskStarted
    # carries the FULL prefixed name ("do_compile") in its own _task attribute -
    # so restore the prefix here or the join silently fails and no badge appears.
    evt._package = d.getVar('PF')
    evt._task = 'do_' + (d.getVar('BB_CURRENTTASK') or '')
    bb.event.fire(evt, d)

    # Plain-log half: reaches kas.log/console.log via bakar's PTY capture. The
    # note's task name is not joined against anything, so the bare BB_CURRENTTASK
    # form is fine here.
    bb.note('%s %s: cache=%s' % (d.getVar('PF'), d.getVar('BB_CURRENTTASK'), backend))
}

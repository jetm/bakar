#!/bin/sh
# Linker timing wrapper for meta-bakar-mold.
#
# Installed by mold.bbclass as BOTH ld.mold and ld.bfd inside the -B wrapper
# dir (RECIPE_SYSROOT_NATIVE). The compiler driver invokes it in place of the
# real linker; it times the real link and appends one JSON record per link to
# the build-global log so the mold vs bfd arms can be compared symmetrically.
#
# Invariants:
#   * NEVER break the link. Any failure in the instrumentation is swallowed and
#     the real linker's exit code is always propagated.
#   * NEVER recurse into itself: the real linker is resolved from PATH with the
#     wrapper's own directory removed (A14).
#   * Since the real linker must run BEFORE we can log its duration, it runs as
#     a child (not exec) on the logging path; exec is only used on the
#     no-logging fast path where timing is not wanted.
#
# Shared link-log contract (tasks 1.3 / 4.2 / 7.1): one JSON object per line
# with keys {linker, recipe, output, wall_ms, nproc, loadavg, threads},
# appended with a single O_APPEND (>>) write so parallel links do not
# interleave.

# Optional absolute path to the real linker, baked in at staging time (A14
# fallback). Empty by default -> resolve from PATH below.
REAL_LINKER=""

# Invoked linker name, e.g. ld.mold or ld.bfd. This is both the "linker" field
# and the basename of the real linker to resolve.
self="$0"
linker="${self##*/}"

# Absolute directory the wrapper lives in, so it can be excluded from the PATH
# search that finds the real linker.
case "$self" in
    */*) selfdir="${self%/*}" ;;
    *)   selfdir="." ;;
esac
selfdir="$(cd "$selfdir" 2>/dev/null && pwd)" || selfdir=""

# Resolve the real linker: first PATH entry (canonicalised) whose directory is
# not the wrapper's own dir. This is what prevents infinite self-recursion.
real="$REAL_LINKER"
if [ -z "$real" ]; then
    oldifs="$IFS"
    IFS=":"
    for d in $PATH; do
        [ -n "$d" ] || d="."
        # Cheap check first: only canonicalise the dir once a candidate linker
        # actually lives there and is executable (and not a directory).
        [ -x "$d/$linker" ] && [ ! -d "$d/$linker" ] || continue
        cd_d="$(cd "$d" 2>/dev/null && pwd)" || continue
        [ "$cd_d" = "$selfdir" ] && continue
        real="$cd_d/$linker"
        break
    done
    IFS="$oldifs"
fi

if [ -z "$real" ]; then
    echo "ld-timing-wrapper: cannot locate real '$linker' on PATH" >&2
    exit 127
fi

# Fast path: no log configured -> exec the real linker directly, no logging.
# Never break the link when instrumentation is disabled.
if [ -z "${BAKAR_MOLD_LINKLOG:-}" ]; then
    exec "$real" "$@"
fi

# --- Logging path -----------------------------------------------------------

# Extract the output file (-o value) and the mold thread count, if present.
output=""
threads=null
expect=""
for a in "$@"; do
    if [ "$expect" = "o" ]; then
        output="$a"
        expect=""
        continue
    fi
    if [ "$expect" = "tc" ]; then
        case "$a" in
            '' | *[!0-9]*) : ;;
            *) threads="$a" ;;
        esac
        expect=""
        continue
    fi
    case "$a" in
        -o) expect="o" ;;
        -o?*) output="${a#-o}" ;;
        --thread-count=* | -thread-count=*)
            v="${a#*=}"
            case "$v" in
                '' | *[!0-9]*) : ;;
                *) threads="$v" ;;
            esac
            ;;
        --thread-count | -thread-count) expect="tc" ;;
    esac
done

# Recipe (PN) is embedded in the workdir path: .../work/<target_sys>/<PN>/<PV>/...
pwd_val="${PWD:-$(pwd 2>/dev/null)}"
recipe=""
case "$pwd_val" in
    */work/*)
        rest="${pwd_val#*/work/}"
        rest="${rest#*/}"
        recipe="${rest%%/*}"
        ;;
esac
[ -n "$recipe" ] || recipe="${pwd_val##*/}"

# nproc and loadavg covariates so contended-parallel numbers can be normalised.
nproc="$(nproc 2>/dev/null)" || nproc=""
case "$nproc" in
    '' | *[!0-9]*) nproc=null ;;
esac

loadavg=null
if [ -r /proc/loadavg ]; then
    read -r la _ </proc/loadavg 2>/dev/null || la=""
    case "$la" in
        '' | *[!0-9.]*) : ;;
        *) loadavg="$la" ;;
    esac
fi

# Time and run the real linker as a child so we can log after it returns.
start="$(date +%s%N 2>/dev/null)"
"$real" "$@"
rc=$?
end="$(date +%s%N 2>/dev/null)"

case "$start" in '' | *[!0-9]*) start="" ;; esac
case "$end" in '' | *[!0-9]*) end="" ;; esac
if [ -n "$start" ] && [ -n "$end" ] && [ "$end" -ge "$start" ]; then
    wall_ms=$(((end - start) / 1000000))
else
    wall_ms=0
fi

# Escape a value for inclusion in a JSON string (backslash then doublequote).
# Only "output" needs it: it is a build-supplied path that can carry a quote or
# backslash, whereas "linker" and "recipe" are a linker basename and a PN, which
# the toolchain guarantees cannot contain either character.
esc() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

line="$(printf '{"linker":"%s","recipe":"%s","output":"%s","wall_ms":%s,"nproc":%s,"loadavg":%s,"threads":%s}' \
    "$linker" "$recipe" "$(esc "$output")" \
    "$wall_ms" "$nproc" "$loadavg" "$threads")"

# Single O_APPEND write keeps parallel-link records from interleaving. A logging
# failure must never fail the link.
printf '%s\n' "$line" >>"$BAKAR_MOLD_LINKLOG" 2>/dev/null || true

exit "$rc"

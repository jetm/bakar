SUMMARY = "mold: a modern, high-speed drop-in replacement for ld.bfd/gold/lld"
HOMEPAGE = "https://github.com/rui314/mold"
DESCRIPTION = "mold is a faster drop-in replacement for existing Unix linkers. \
It is several times quicker than the LLVM lld linker and produces byte-identical \
output for a given input, making it usable inside reproducible OE builds."

# mold itself is MIT since v2.0 (2023-07-26). The tree bundles five third-party
# components under third-party/, each with its own license, so the aggregate
# LICENSE is NOT bare MIT (a bare "MIT" would fail QA license-checksum coverage,
# handoff C4):
#   mold      -> MIT
#   mimalloc  -> MIT
#   oneTBB    -> Apache-2.0
#   zlib      -> Zlib
#   xxhash    -> BSD-2-Clause
#   blake3    -> CC0-1.0 (dual-licensed CC0-1.0 OR Apache-2.0; CC0 arm taken here)
LICENSE = "MIT & Apache-2.0 & Zlib & BSD-2-Clause & CC0-1.0"

# LIC_FILES_CHKSUM covers mold plus the bundled third-party components carried
# in-tree at v2.42.1. mold 2.42.1 vendors these directly (no git submodules), so
# the paths are ${S}-relative in-tree files. blake3 is dual CC0-1.0 OR Apache-2.0
# and ships no bare LICENSE - the CC0 arm (LICENSE_CC0) is taken to match the
# CC0-1.0 term in LICENSE above. zstd (BSD-3-Clause) and rust-demangle are also
# vendored but not yet enumerated here; that is a known compliance gap, not a
# checksum-QA failure.
#
# zlib's checksum moved from the 2.41.0 pin (b51a40671bc46e961c0498897742c0b8)
# on this bump: a copyright-year-only edit (1995-2022 -> 1995-2026), same Zlib
# terms, confirmed by diffing both revisions' third-party/zlib/LICENSE before
# updating this line. Re-diff on every future bump rather than assuming a
# checksum change is always this benign.
LIC_FILES_CHKSUM = "\
    file://LICENSE;md5=3fb62e3fb2aa1c0f7d16e43be0107e99 \
    file://third-party/mimalloc/LICENSE;md5=fade5fb11a9703a4216c1b5133efdc7f \
    file://third-party/tbb/LICENSE.txt;md5=86d3f3a95c324c9479bd8986968f4327 \
    file://third-party/zlib/LICENSE;md5=66e4e749bb11d80fc7ba510a9ce99534 \
    file://third-party/xxhash/LICENSE;md5=13be6b481ff5616f77dda971191bb29b \
    file://third-party/blake3/LICENSE_CC0;md5=65d3616852dbf7b1a6d4b53b00626032 \
"

# Fetch mold's tagged release. v2.42.1 vendors its third-party deps directly
# in-tree (mimalloc/tbb/zlib/xxhash/blake3/zstd/rust-demangle) with no git
# submodules, so gitsm degrades to a plain git checkout here. nobranch=1 pins
# the exact SRCREV and skips kas/bitbake's branch-containment check
# unconditionally - whether a given tag commit is reachable from upstream's
# main branch varies release to release (v2.41.0's wasn't; v2.42.1's is, per
# `gh api repos/rui314/mold/compare/main...v2.42.1` reporting "behind"), so
# pin on SRCREV rather than re-deriving this per bump. Do NOT vendor these
# sources into the bakar repo and do NOT add a network fetch outside SRC_URI.
#
# v2.42.1 is a lightweight tag pointing directly at this commit
# (git ls-remote --tags https://github.com/rui314/mold v2.42.1).
SRCREV = "9b376bc6a9899d4a16b41777de1f013989459fbc"
SRC_URI = "gitsm://github.com/rui314/mold.git;protocol=https;nobranch=1"

# mold requires a C++20 host compiler (GCC 12+ / Clang 15+). The bakar doctor gate
# probes the mode-appropriate build compiler for C++20 before the build starts so
# this fails in seconds rather than 40 minutes into do_compile (handoff S6).
inherit cmake

# Build the bundled third-party libraries rather than the host's; a native mold
# must not link the build host's zlib/tbb to stay reproducible across builders.
EXTRA_OECMAKE = "\
    -DMOLD_USE_SYSTEM_TBB=OFF \
    -DMOLD_USE_SYSTEM_MIMALLOC=OFF \
    -DMOLD_USE_MIMALLOC=ON \
    -DCMAKE_BUILD_TYPE=Release \
"

# mold's CMake install stages the `mold` binary plus the `ld.mold` compatibility
# symlink into ${bindir}. For the native variant that lands in the native bindir
# (STAGING_BINDIR_NATIVE), which the mold.bbclass -B<wrapper-dir> discovery stages
# from. The default do_install from `inherit cmake` covers this.

BBCLASSEXTEND = "native"

# ---------------------------------------------------------------------------
# A12 validation (runs later on the wrynose OE tree, NOT in this repo):
# mold must write an identifiable stamp into the output .comment section so the
# post-build mold_verify gate (tasks 7.2/8.1) can key present/absent on it.
#
#   bitbake mold-native
#   printf 'int main(void){return 0;}\n' > /tmp/hello.c
#   $(bitbake -e mold-native | sed -n 's/^STAGING_BINDIR_NATIVE="\(.*\)"/\1/p')/ld.mold --version
#   gcc -fuse-ld=mold /tmp/hello.c -o /tmp/hello
#   readelf -p .comment /tmp/hello | grep -i mold   # capture the EXACT stamp string
#
# Record that exact stamp string for tasks 7.2 and 8.1 to match. If mold emits no
# default .comment stamp, set MOLD_DEBUG=1 (embeds the linker cmdline in .comment)
# and key the gate on that instead (design A12 fallback).
# ---------------------------------------------------------------------------

# Host-mode build isolation: qemu-system-native's meson probes rutabaga_gfx as
# an 'auto' feature and finds the build host's librutabaga_gfx_ffi (e.g. CachyOS
# ships rutabaga_gfx 0.1.75 as /usr/include/rutabaga_gfx/rutabaga_gfx_ffi.h +
# /usr/lib/librutabaga_gfx_ffi.so), silently enabling it even though the recipe
# never lists rutabaga in PACKAGECONFIG. hw/display/virtio-gpu-rutabaga.c then
# includes rutabaga_gfx/rutabaga_gfx_ffi.h from the host's default /usr/include -
# a path sccache-dist does not ship to a build server (only -I-referenced
# headers are packaged into the dist job), so the compile fails on the secondary
# node with "rutabaga_gfx/rutabaga_gfx_ffi.h: No such file or directory". A
# native binary linking a host graphics lib is non-reproducible regardless of
# distribution, so pin the accidentally-detected feature off. Mirrors the
# rpm-plugins host-leak guard in this layer.
EXTRA_OECONF:append = " --disable-rutabaga-gfx"

# `download-cache-prune`: the `prune-downloads` verb: the three removal classes, how each is verified, the per-file lock, dry-run and apply behaviour, and what is reported.

**Delivered by:** dl-dir-space-sharing
**Modules touched:** other, tests

## Delivery Note

Invocation: bakar cache prune-downloads --apply, run after a dry run (bakar cache prune-downloads) on a builder with dl_dir and dl_mirror_url set; a host-mode build then uses the mounted store through the [build] key dl_mirror_paths in ~/.config/bakar/config.toml
Precondition: dl_dir or DL_DIR set; the hub class needs dl_mirror_url and a reachable hub; the file mirror needs the hub's download store mounted read-only on the builder (the chezmoi share applied with fleet update on PC1, then pc2 and pc3), dl_mirror_paths naming its downloads and incoming directories, and a host-mode build
Success signal: the prune prints one block per class with counts, sizes and kept reasons and exits 0, and with --apply the verified files are gone; a host-mode build lists the hub-mount overlay, bakar getvar PREMIRRORS shows the file:// entries before the http store, a fetched source appears in the downloads directory as a symbolic link into the mirror directory, and bakar doctor shows the dl-mirror-paths-ready and dl-dir-size rows
Silent failure: a container build ignores dl_mirror_paths without any message and keeps copying mirror hits from the http store, so the saving never appears there

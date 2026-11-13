# `remote-build-pull`: a pull step copies only the described files into a private staging directory, validates names, types and sizes before and after the copy, compares checksums, describes the host again to catch a live or changed build, and removes the staging directory on every exit path

**Delivered by:** avocado-feed-first-cross-node-publish
**Modules touched:** other, tests

## Delivery Note

Invocation: `bakar feed refresh <kas.yml> --recipe <recipe> --from <host> --run <run-id> --project <dir>` pulls the named recipes' RPMs and recipe data from the finished remote build and publishes them into the local delta feed, then prints the overlay stanzas; `bakar feed describe <run-id> --recipe <recipe>` is the hidden verb it runs on the host.
Precondition: the same bakar version and content on the local node and the build host, a finished build dispatched with `bakar build --on <host>` by a bakar that forwards `BAKAR_DISPATCH_KAS` and `BAKAR_DISPATCH_WORKSPACE`, ssh and rsync reaching the host through the existing dispatch options, and the run id that dispatch prints as `remote run-id`.
Success signal: exit 0, the local delta holds only the named recipes with matching `recipe-map.json` and a `delta.json` `source` record, the overlay stanzas print, and `check-build-path` flips from `package_miss` to `feed_hit` with no bakar process started.
Silent failure: a clone that moved after the build can make the pin comparison refuse a good build, and a build started on the host between the second description and the swap is not detectable; content is structurally validated and checksummed but not authenticated, so a compromised host could publish forged packages.

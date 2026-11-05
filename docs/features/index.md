# Features

`bakar init` offers these features. Each page lists what your host needs, how to enable the feature and how to turn it off. Tiers follow the catalog in `src/bakar/setup/features.py`.

## Host-local

| Feature | Summary | Page |
|---------|--------------|------|
| ccache | Compiler cache | [ccache.md](ccache.md) |
| hashserv | Hash equivalence server | [hashserv.md](hashserv.md) |
| psi | PSI autocalibration | [psi.md](psi.md) |
| scope | systemd scope isolation | [scope.md](scope.md) |
| nfs-cache | Shared sstate and downloads | [nfs-cache.md](nfs-cache.md) |
| uninative | Uninative | [uninative.md](uninative.md) |

## Needs an endpoint

| Feature | Summary | Page |
|---------|--------------|------|
| sstate-mirror | sstate mirror | [sstate-mirror.md](sstate-mirror.md) |
| dl-mirror | Downloads mirror | [dl-mirror.md](dl-mirror.md) |
| cache-push | Cache push | [cache-push.md](cache-push.md) |
| sccache-dist | Distributed sccache | [sccache-dist.md](sccache-dist.md) |
| central-node | Central node | [central-tier.md](central-tier.md) |
| central-client | Central client | [central-tier.md](central-tier.md) |

## Multi-host

| Feature | Summary | Page |
|---------|--------------|------|
| remote-dispatch | Dispatch builds to another host with `--on` | [remote-dispatch.md](remote-dispatch.md) |
| release-workspaces | One workspace per release from a path template | [release-workspaces.md](release-workspaces.md) |
| feed | Publish built packages as a shared feed | [feed.md](feed.md) |

# Central tier

The central tier shares one hash equivalence server and one PR server across a cluster of builders, so every builder's `BB_HASHSERVE` and `PRSERV_HOST` point at the same services instead of per-workspace daemons. It has two features, one per role: `central-node` runs the services, and `central-client` points a builder at a node that already runs them. A host takes one role, not both: each feature excludes the other.

Both roles turn cluster mode on, and both write the same three keys: `build.bb_hashserve`, `build.prserv_host` and `build.cluster`. Cluster mode is turned on with the endpoints: `build.cluster` is set to `true` together with them, not as a separate step. Once it is on, a configured sstate or downloads directory must be a shared mount (hard requirement, doctor check `shared-mounts`); see [nfs-cache](nfs-cache.md).

## What your host needs

### Central node

1. The `avocado-buildservices` package, which provides the systemd units `avocado-hashserv.service` and `avocado-prserv.service` (hard requirement, probe `central-units`). The setup action fails its preflight when `systemctl cat` cannot find either unit.
2. A reachable PostgreSQL (hard requirement, probe `central-postgres`). The prompts default to host `localhost` and port `5432`, and the probe is a TCP connect to that address.
3. The PostgreSQL roles `hashserv` and `prserv`, created by you before enabling. `bakar init` creates only the two databases, `hashserv` and `prserv`, and never roles or passwords. Its bootstrap connects as a role named like its database with the role name as the password (`PGPASSWORD=<name> psql -U <name> -d <name>`), and runs `createdb -O <name> <name>` only when that connect fails. The database URLs therefore default to `postgres://hashserv:hashserv@<pg_host>:<pg_port>/hashserv` and `postgres://prserv:prserv@<pg_host>:<pg_port>/prserv`. Without the roles the bootstrap cannot create the databases.
4. A bind address that your peers can reach, such as the node's cluster IP. Both services bind it, and the endpoints written to your config use it. There is no default; the feature requires it.
5. A shared sstate or downloads directory, if you configured one, must be a shared mount (see above).

### Central client

1. The central node's hashserv endpoint, as `build.bb_hashserve` (hard requirement, doctor check `central-hashserv`).
2. The central node's PR server as `host:port`, as `build.prserv_host` (hard requirement, doctor check `central-prserv`).
3. A shared sstate or downloads directory, if you configured one, must be a shared mount (see above).

## Enable

Interactive: run `bakar init` and select central-node or central-client.

Non-interactive, node: `bakar init --feature central-node --set central-node.bind_host=<addr>`

Optionally add `--set central-node.pg_host=<host>` and `--set central-node.pg_port=<port>` when PostgreSQL is not on `localhost:5432`. These are inputs, not config keys: init uses them to build the database URLs and does not write them.

Non-interactive, client: `bakar init --feature central-client --set build.bb_hashserve=<endpoint> --set build.prserv_host=<host:port>`

The node writes `build.bb_hashserve`, `build.prserv_host` and `build.cluster`, with `build.cluster` fixed to `true`. The client prompts for the first two and also fixes `build.cluster` to `true`. The node-side setup action also runs `systemctl --system enable --now avocado-hashserv.service avocado-prserv.service`, which needs root.

## Turn off

Run `bakar init --disable central-node` or `bakar init --disable central-client`. Either unsets `build.bb_hashserve`, `build.prserv_host` and `build.cluster` in your user config.

Turning off a node leaves its services running. To stop and disable them, run:

```bash
sudo systemctl disable --now avocado-hashserv.service avocado-prserv.service
```

## See also

- [nfs-cache](nfs-cache.md)
- [hashserv](hashserv.md), whose per-workspace daemon the central endpoint overrides
- `bakar doctor`

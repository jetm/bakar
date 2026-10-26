# bakar sync

Run manifest-driven source sync without building. Equivalent to the first half of `bakar build`: doctor, then `repo init+sync` (NXP) or `oe-layertool populate` (TI), then setup-env.

## Synopsis

```text
bakar sync [OPTIONS]
bakar sync <kas file> [--on HOST] [--dry-run] [-w WORKSPACE]
bakar sync --revert FILE [--on HOST]
```

The second and third forms are the pin sync for a meta-avocado release workspace; see [Pin sync for a release workspace](#pin-sync-for-a-release-workspace).

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--machine` | `-m` | Target machine |
| `--distro` | `-d` | Distro |
| `--image` | `-i` | Image target |
| `--manifest` | `-f` | Manifest filename (NXP `.xml` or TI `.txt`) |
| `--branch` | `-b` | Branch override |
| `--clean` | | Remove `<bsp>/build/` before syncing |
| `--show-layers` | | Print layer git hashes after sync |
| `--dry-run` | `-n` | Print the sync commands that would run, then exit without syncing. With a kas file it prints what the pin sync would do with each repository and changes nothing; see [Pin sync for a release workspace](#pin-sync-for-a-release-workspace) |
| `--dry-run-script` | | Write a runnable bash script to PATH instead of syncing; use `-` for stdout. Distinct from `--dry-run`: this produces an executable script whose sync step matches the workspace family (repo for NXP, oe-layertool for TI, kas-container checkout for bbsetup). |
| `--workspace` | `-w` | Workspace root override |
| `--on` | | With a kas file (or `--revert`), run the pin sync in the release workspace on this host (ssh alias or `user@ip`) instead of locally. No bakar install is needed there |
| `--revert` | | Put the clones named in a revert record (`<workspace>/.bakar/sync-revert/*.tsv`, written by a pin sync) back on their recorded `HEAD`s. With `--on`, FILE is an absolute path on that host. See [Reverting](#reverting) |

**Global option:** `--hide-doctor-report`, placed before the subcommand
(`bakar --hide-doctor-report sync ...`), runs the doctor checks but prints
output only for build-blocking issues. Set `[build] show_doctor_report = false`
for the same effect on every invocation.

## Examples

```bash
# Sync NXP sources to a new manifest version
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart

# Sync and confirm which commits landed
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --show-layers

# Sync TI sources
bakar sync -f processor-sdk-10.1.0.8-config_var1.txt -m am62x-var-som

# Force a clean re-sync (wipe build/ first)
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --clean

# Emit a runnable sync script to stdout without running the sync
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script -

# Write the sync script to a file
bakar sync -f imx-6.12.49-2.2.0.xml -m imx8mp-var-dart --dry-run-script sync.sh
```

## Notes

- bitbake-setup workspaces are initialized externally via `bitbake-setup init`; `bakar sync` exits 2 for them.
- `--clean` removes `build/` through the same check as `bakar clean`. It refuses with exit 1 when a peer node owns the build marker or a build is running on this node, naming the marker and the manual clear (delete the marker and the `bitbake.lock` beside it by hand once no build runs on any node). When the lock cannot be attributed, it warns and proceeds. There is no force flag.
- bakar detects manifest drift (wrong manifest, wrong branch, SHA drift) and forces a full re-sync when it detects it. The drift check is independent of the doctor pre-flight; `--hide-doctor-report` hides the doctor report but never suppresses the drift check.

## Pin sync for a release workspace

```text
bakar sync <kas file> [--on HOST] [--dry-run] [-w WORKSPACE]
bakar sync --revert FILE [--on HOST]
```

Giving a kas file inside a meta-avocado checkout switches `sync` to a different job: it moves the shared clones of that release workspace onto the commits the kas file pins. Without the positional, `bakar sync` behaves as described above. Use `bakar sync <kas>` when a release workspace went stale: its clones sit off the commits the kas file pins, so a build would run against the wrong sources.

- The workspace is the one `bakar build <kas file>` uses (routed by release, `-w` wins). With `--on HOST` it is the release workspace on the host, resolved as `bakar build --on HOST` resolves it, and `-w` is refused. No bakar install is needed on the host.
- The kas file is a single file inside a meta-avocado checkout. `main.yml:overlay.yml` is refused, and so is a file outside a meta-avocado checkout.
- `--dry-run` runs a read-only probe and prints one line per pinned repository as `<name>: <verdict>`, where the verdict is `would move to <commit>` (12 characters), `already at its pin` (followed by `(has local changes)` for a dirty clone), `HEAD starts with its abbreviated pin, which a dry run cannot resolve` (an abbreviated pin `HEAD` starts with: only the real sync asks git whether the prefix names that one commit), `missing`, `left alone because of local changes`, `left alone, state unreadable` or `cannot be synced (<reason>)`. An entry with no commit pin gets `<name>: no commit pin, sync leaves it alone and a build refuses it`. After the per-repository lines the dry run predicts the state the sync would leave, treating every clone it would move as clean at its pin, and re-checks it with the rules a build applies. When something would still be refused it prints `after the sync <n> repositories would still block a build, and bakar sync cannot clear it:` (`1 repository would still block ...` for one) followed by one blocker line per repository, and exits 1, exactly as the real sync does. An entry with no commit pin is such a blocker, so it makes the dry run exit 1. A `left alone ...` or `cannot be synced ...` verdict also makes it exit 1, because the real sync reports that repository as `LEFT` or `FAIL` and exits 1. It changes nothing and takes no lock. When the clones cannot be inspected it prints `cannot inspect the clones: ...` and exits 1.
- The re-check, in the dry run and after a real sync, pairs a diverged `openembedded-core` with a frozen `bitbake` the way the build does: only when a local `bakar build` of the kas file would run in host mode. Container mode does not pair, so a diverged `openembedded-core` is a blocker there. With `--on` the host's own build mode is not visible, so the check assumes host mode; a container build on the host may still refuse an `openembedded-core` this check lets through.

### What moves and what never does

- Only a clone that is clean and off its pin moves, by `git checkout --detach <pin>`. If the pin is not in the clone, it is fetched first.
- A clone with local changes is left alone (`LEFT`). A missing clone is left for kas to create (`MISSING`). A side checkout, such as the one a diverged `openembedded-core` may take under the `reuse` repository policy, is not a pinned clone and is never touched.
- Branch pointers never move. The sync changes `HEAD` and the working tree of a clone and nothing else: no branch, ref, config or file outside the clone, and no force, reset, clean or stash.
- A pin that is not hexadecimal, a url that begins with `-`, and a name or path containing a tab or newline are reported as `FAIL` without being run. An abbreviated hexadecimal pin (kas accepts one) is compared but never checked out: a clone is `AT PIN` only when git in that clone resolves the pin to its `HEAD`. A clone off it, or one where two commits share the prefix so git cannot resolve it, is `FAIL <name> pin is not a full 40-character commit id` and stays where it was. The pin's length is checked before local changes, so a dirty clone off an abbreviated pin reports that `FAIL` rather than `LEFT`.
- A clone path is confined to the workspace. When a repository's path resolves outside the workspace (`FAIL <name> path is not under the workspace`) or is not the top level of its own git repository (`FAIL <name> path is not the top of a clone`), the sync moves nothing there. A plain directory nested inside another clone is refused this way, so git never acts on the enclosing repository.
- The checkout refuses to overwrite an ignored file that the target commit tracks: the row is `FAIL <name> checkout failed: <git's message>` and the clone stays where it was.
- Only `git status` output counts as local changes; a warning git prints on stderr does not make a clone dirty. Every status call runs with `--no-optional-locks`, so reading a clone never rewrites its index. A clone whose status cannot be read is `FAIL <name> cannot read status`, at its pin or not.
- The branch a clone was on is read from its full ref and recorded as the branch name, so the record and the `MOVED` line name the branch even when a tag shares its name.

### The running-build refusal

Before any clone moves, the sync takes the whole-run lock (`<workspace>/.bakar/repos-in-use.lock`) and the repository lock (`<workspace>/.bakar/repos.lock`), then checks every build directory's `bitbake.lock`. Any bakar session that builds, shells or dumps under the `reuse` repository policy holds the whole-run lock for its run, so a live build or `bakar shell` on the workspace refuses the whole run, and nothing moves. The refusal prints one line:

```text
REFUSED a bakar build or shell session holds <workspace>/.bakar/repos-in-use.lock
REFUSED another run holds <workspace>/.bakar/repos.lock
REFUSED a build holds <workspace>/<dir>/build/bitbake.lock (build directory <workspace>/<dir>)
REFUSED cannot open <workspace>/<dir>/build/bitbake.lock
```

A lock file that merely exists refuses nothing; only a live holder does. A lock file the sync cannot open (for example one with mode 000) refuses with `cannot open <lock>` rather than being reported as a live build. The same refusal applies to `--revert`. The whole-run and repository locks stay held exclusively until every clone has moved.

### Output

One line per pinned repository, in kas file order:

| Line | Meaning |
|------|---------|
| `MOVED <name> <branch> <old> -> <pin>` | The clone was checked out detached at its pin. `<branch>` is the branch it was on, or `-` when detached; `<old>` and `<pin>` are 10 characters. |
| `AT PIN <name>` | `HEAD` already starts with the pin. `AT PIN <name> (has local changes)` when the clone is dirty; the sync does not touch it. |
| `MISSING <name> (kas clones it at build time)` | No clone exists; kas creates it. |
| `LEFT <name> has local changes, not moved` | Dirty clone, left alone. |
| `FAIL <name> <detail>` | The clone could not be read, fetched or checked out, the revert record could not be written, or the entry was rejected. |

A `MOVED` line is followed directly by `build dirs pinning <name> at <old>: <dir>, ...` when build directories have an `avocado-bakar.yml` that still names the old commit of that repository. Those build directories were generated against the old commit.

After the repository lines, in this order and only when they apply:

- `revert record: <path>` names the record described below.
- `sync did not finish after <N>s and may still be running; check before re-running` (with `--on`: `sync did not finish after <N>s on <host> and may still be running there; check before re-running`), printed when the script ran past its 3600-second bound. A local sync keeps the rows and the `revert record:` line it printed before the bound; over ssh that partial output cannot be recovered, and the script may keep running on the host. Check the clones, and the `.bakar/sync-revert/` directory, before running the sync again. The run exits 1.
- A `bitbake moved:` line, printed when the `bitbake` clone moved: restart the servers that loaded the old one with `bakar hashserv stop -w <workspace>` then `bakar hashserv start -w <workspace>`, and `bakar prserv stop -w <workspace>` then `bakar prserv start -w <workspace>`, naming the synced workspace (shell-quoted). With `--on HOST` the line reads `restart the servers that loaded the old one, on <host>, with ...`: the commands run on that host. A daemon that loaded the old bitbake keeps running it until restarted.
- `after the sync <n> repositories still block a build, and bakar sync cannot clear it:` (`1 repository still blocks ...` for one) followed by one line per repository in the form `<name> (<path>): <reason>; HEAD <commit12>, pin <commit12>, clean|dirty, pin commit present|absent` (`unknown` when git could not say, `none` for a missing commit), printed when the finished sync is re-checked with the rules a build applies and something would still be refused: a dirty `bitbake` or `openembedded-core` (even at its pin), an entry with no usable pin, or a dirty side checkout. These need a hand edit. If the re-check cannot run, the line is `cannot re-check the clones after the sync: <reason>`. The sync exits 1 in both cases.

The run ends with the completion line:

```text
batch complete: <examined>/<total> repos, moved=<n> failed=<n>
```

A shortfall between `examined` and `total` means the script did not finish, and the run exits 1 even when every printed line looks clean. If the script printed to stderr and did not complete, the first 500 characters follow as `sync script stderr: ...`.

### The revert record

The first time a sync moves a clone, it creates `<workspace>/.bakar/sync-revert/<YYYYMMDDTHHMMSS>-<pid>-<random>.tsv`, before the move, and appends one line per moved clone. The record is created with `mktemp` under a name nobody can predict, so a file or symlink planted in that directory is never followed or appended to; when it cannot be created the repository is `FAIL <name> cannot create the revert record` and does not move. Each line has five tab-separated fields:

```text
<name>	<path>	<head before the move>	<branch before the move, or ->	<pin the sync set>
```

A record written by an earlier version has four fields and no pin; `--revert` still reads it, without the check on the pin described below.

A run that moves nothing writes no record. With `--on`, the record lives on the host.

### Reverting

`bakar sync --revert FILE` puts each clone named in the record back on its recorded `HEAD`. FILE must sit directly inside `<workspace>/.bakar/sync-revert/`; with `--on HOST` it is a path on that host and must be absolute (`--revert FILE on a host must be an absolute path`, exit 2), because a relative one would resolve against the login directory. The revert checks that the record exists and is readable before it takes any lock, so a missing or unreadable record creates nothing in the workspace and refuses. Each clone gets the same confinement as the sync: its path must resolve under the workspace and be the top level of its own git repository. A clone returns to its recorded branch only when that branch still points at the recorded `HEAD`; otherwise it is checked out detached at that commit. One line per record line:

| Line | Meaning |
|------|---------|
| `RESTORED line <n> on the recorded branch` | Back on the branch, at the recorded `HEAD`. |
| `DETACHED line <n> recorded HEAD was detached` | The record had no branch; checked out detached. |
| `DETACHED line <n> recorded branch moved or is gone` | The branch no longer points at the recorded `HEAD`; checked out detached. |
| `DIRTY line <n> local changes, not restored` | Left alone. |
| `MOVED_ON line <n> clone is no longer at the commit the sync set, not restored` | Left alone: the clone's `HEAD` is neither the recorded pin nor the recorded head, so commits made since the sync would be orphaned by the checkout. A clone still at the recorded head (its sync checkout failed) is restored normally. Only a record with a pin field is checked. |
| `OUTSIDE line <n> path is not under the workspace` | Refused; the record never touches a path outside the workspace. |
| `INVALID line <n> <reason>` | Malformed line: not four or five fields, a pin or head that is not 40 hexadecimal characters, an empty path, or an invalid branch name. |
| `FAIL line <n> <detail>` | Missing clone, a path that is not the top of a clone, unreadable status, an unreadable `HEAD` (`cannot read HEAD`, checked only for a record with a pin field), recorded commit not in the clone, or a failed checkout, including one that would overwrite an ignored file the target commit tracks. Nothing is checked out. |

It ends with `batch complete: <examined>/<total> repos, restored=<n> failed=<n>`. A missing record file prints `REFUSED revert record <path> not found`, and an unreadable one `REFUSED revert record <path> is not readable`. The running-build refusal above applies.

### Exit codes and refused combinations

- Exit 0 when every existing clone ends at its pin (or, for `--revert`, every line is `RESTORED` or `DETACHED`). A `--dry-run` exits 0 when every repository would end at its pin or is missing and nothing would still block a build after the sync.
- Exit 1 when any clone was left alone, failed, was rejected, the run did not complete or timed out, the sync was refused by a lock, the release workspace on an `--on` host could not be checked (`cannot sync on <host>: ...`), or the re-check after the sync finds a repository a build would still refuse. A `--dry-run` exits 1 when the clones cannot be inspected, when a repository would be left alone or cannot be synced, or when its prediction finds a repository that would still block a build, which includes any entry with no commit pin.
- Exit 2 for usage errors, which return before anything is written.

Refused combinations, each printed before any work starts:

- The kas file with `--machine`, `--distro`, `--image`, `--manifest`, `--branch`, `--clean`, `--show-layers` or `--dry-run-script`.
- `--on` without a kas file (or `--revert`).
- `-w` with `--on` and a kas file: the release maps to the workspace on the host, so a named one would be ignored.
- `--revert` with a kas file, `--dry-run`, `-w`, or any of the manifest-form flags above. `--revert` takes no `-w`: the workspace comes from the record's path.
- `--revert FILE` with `--on` when FILE is not an absolute path: `--revert FILE on a host must be an absolute path`.
- A kas file outside a meta-avocado checkout: `the pin sync supports meta-avocado release workspaces only`.
- An `--on` host that begins with `-`.

### Pin sync examples

```bash
# See what a pin sync would do in the release workspace on this machine
bakar sync meta-avocado/kas/machine/qemux86-64.yml --dry-run

# Move the stale clones on a build node
bakar sync meta-avocado/kas/machine/qemux86-64.yml --on pc2

# Undo it from the record the sync printed
bakar sync --revert /path/to/workspace/.bakar/sync-revert/20261026T101500-4242-a1B2c3.tsv --on pc2
```

## See also

- [build.md](build.md) - full pipeline including sync
- [layers.md](layers.md) - inspect layer hashes after sync

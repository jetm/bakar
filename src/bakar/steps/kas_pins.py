"""Read a meta-avocado kas file's merged pins without touching any workspace.

This module is the only place in bakar that may pass ``finish_setup_repos``
to kas. Skipping it is safe here because the dump runs in a throwaway, empty
``KAS_WORK_DIR``: kas has nothing to move, and an include needing a repository
other than the kas file's own checkout is reported as a refusal instead of
being cloned. A build must never skip it, since kas would then stop cloning
any missing repository that no include names.

The three skips do not stop kas from cloning a repository an include names, so
the includes are walked first and any repository other than ``meta-avocado``
is refused before kas runs.

When extra files are merged, a pin of the checkout is merged after them so
that an overlay that names ``meta-avocado`` cannot move it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from bakar import bsp_detect
from bakar.gitutil import git_stdout
from bakar.release_workspace import ReleaseRoutingError, release_key_from_merged

if TYPE_CHECKING:
    from collections.abc import Sequence


_SKIPS = ("repos_checkout", "finish_setup_repos", "repos_apply_patches")
_MISSING_MARKER = "Could not fetch all repos needed by includes"
_MISSING_PREFIX = "Missing repos:"
_DUMP_TIMEOUT_S = 120
_GIT_READ_TIMEOUT_S = 60
# The repository name meta_avocado_wrapper_text gives the checkout.
_OWN_REPO = "meta-avocado"
# Name prefix of the throwaway wrapper written into a build's bsp_root.
PROBE_PREFIX = ".reuse-probe-"
# Appended when kas's error names a path under the throwaway KAS_WORK_DIR: only a
# repository declared by a relative path resolves there, since the dump clones nothing.
_WORK_DIR_HINT = (
    " - kas resolved a repository path against bakar's empty work directory, "
    "so a kas file in the merged stack declares a repository by a relative path"
)


def _dump_argv(wrapper: Path, extras: Sequence[Path] = ()) -> list[str]:
    argv = ["kas", "dump"]
    for step in _SKIPS:
        argv += ["--skip", step]
    argv.append(":".join(str(p) for p in (wrapper, *extras)))
    return argv


def _include_targets(path: Path, checkout: Path) -> tuple[list[Path], list[str]]:
    """Return the include files ``path`` names inside ``checkout`` and the foreign repos it names."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        # kas reads the same file and reports the problem in its own words.
        return [], []
    header = data.get("header") if isinstance(data, dict) else None
    includes = header.get("includes") if isinstance(header, dict) else None
    files: list[Path] = []
    foreign: list[str] = []
    for inc in includes if isinstance(includes, list) else []:
        rel = inc
        if isinstance(inc, dict):
            repo = inc.get("repo")
            if repo is not None and str(repo) != _OWN_REPO:
                foreign.append(str(repo))
                continue
            rel = inc.get("file")
        if not isinstance(rel, str):
            continue
        target = checkout / rel
        # kas falls back to file-relative addressing when the repo-relative file is absent.
        if not target.exists():
            target = path.parent / rel
        files.append(target.resolve())
    return files, foreign


def _foreign_include_repos(kas_yaml: Path, checkout: Path) -> list[str]:
    """Return every repository other than the checkout that ``kas_yaml``'s includes need."""
    checkout = checkout.resolve()
    seen: set[Path] = set()
    foreign: list[str] = []
    pending = [kas_yaml.resolve()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        files, repos = _include_targets(path, checkout)
        foreign += [r for r in repos if r not in foreign]
        pending += reversed(files)
    return foreign


def _missing_repos(stderr: str) -> str:
    for line in stderr.splitlines():
        if _MISSING_PREFIX in line:
            return line.split(_MISSING_PREFIX, 1)[1].strip()
    return ""


def _first_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    # kas logs INFO/WARNING lines before the ERROR that explains the exit.
    for line in lines:
        if "ERROR" in line:
            return line
    return lines[0] if lines else "(no stderr)"


def _failure_context(checkout: Path, extra_files: Sequence[Path]) -> str:
    """Return `` (overlays: ...; checkout at ...)`` for a failed dump, or "" when neither is known."""
    parts: list[str] = []
    if extra_files:
        parts.append("overlays: " + ", ".join(extra.name for extra in extra_files))
    head = git_stdout(checkout, "rev-parse", "HEAD")
    if head:
        parts.append(f"checkout at {head.strip()[:12]}")
    return f" ({'; '.join(parts)})" if parts else ""


def _run_dump(
    kas_yaml: Path, wrapper: Path, extras: Sequence[Path], *, cwd: Path, work_dir: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            _dump_argv(wrapper, extras),
            capture_output=True,
            text=True,
            timeout=_DUMP_TIMEOUT_S,
            check=False,
            cwd=cwd,
            env={**os.environ, "KAS_WORK_DIR": work_dir},
        )
    except FileNotFoundError as exc:
        raise ReleaseRoutingError(f"cannot read the kas pins of {kas_yaml}: kas is not installed ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReleaseRoutingError(
            f"cannot read the kas pins of {kas_yaml}: kas dump did not finish within {_DUMP_TIMEOUT_S}s"
        ) from exc


def read_merged_config(
    kas_yaml: Path,
    *,
    checkout: Path,
    extra_files: Sequence[Path] = (),
    wrap_parent: Path | None = None,
) -> dict:
    """Return the merged kas config of ``kas_yaml`` read from ``checkout``.

    ``extra_files`` are merged after the entry in the given order, as the
    build's own dump merges its overlays. When there are any, a pin of the
    checkout is merged after the extras so that an overlay that names
    ``meta-avocado`` cannot move it.

    With ``wrap_parent`` (the build's ``bsp_root``) the throwaway wrapper is a
    uniquely named file directly in it and the extras are named in place, so
    kas sees the layout of the build's own dump: the same repository for its
    same-repo check, and the same include root, which kas takes from the first
    file's git root or, outside git, from that file's own directory. That is
    why the wrapper is not in a subdirectory: outside git a subdirectory would
    move the root that an overlay's string includes resolve against.

    Without it the wrapper goes to the system temp dir and the extras are
    copied beside it, since kas refuses to concatenate files from two
    repositories and that wrapper sits outside any. Copying changes how the
    extras' string includes resolve, so the build passes ``wrap_parent``.

    Raises ReleaseRoutingError when kas cannot produce it, or when an include
    of ``kas_yaml`` needs a repository other than ``checkout``.
    """
    foreign = _foreign_include_repos(kas_yaml, checkout)
    if foreign:
        raise ReleaseRoutingError(
            f"{kas_yaml} includes files from repositories other than its own checkout ({', '.join(foreign)}); "
            "the release cannot be read without fetching them"
        )
    text = bsp_detect.meta_avocado_wrapper_text(checkout, kas_yaml)
    pin_text = bsp_detect.meta_avocado_pin_text(checkout) if extra_files else None
    out, wrapper, work_dir_names = _dump_wrapped(kas_yaml, text, pin_text, extra_files, wrap_parent)
    return _parse_dump(
        out, kas_yaml, wrapper, work_dir_names, _failure_context(checkout, extra_files) if out.returncode != 0 else ""
    )


def _dump_wrapped(
    kas_yaml: Path,
    text: str,
    pin_text: str | None,
    extra_files: Sequence[Path],
    wrap_parent: Path | None,
) -> tuple[subprocess.CompletedProcess[str], Path, set[str]]:
    """Run the three-skip dump of ``text`` (the wrapper) plus extras and pin, in a throwaway work dir.

    Returns the process, the wrapper path and the names the work dir goes by.
    """
    with tempfile.TemporaryDirectory(prefix="bakar-kas-pins-work-") as work_dir:
        work_dir_names = {work_dir, os.path.realpath(work_dir)}
        if wrap_parent is None:
            with tempfile.TemporaryDirectory(prefix="bakar-kas-pins-") as wrap_dir:
                wrapper = Path(wrap_dir) / "avocado-wrapper.yml"
                wrapper.write_text(text)
                extras: list[Path] = []
                for index, extra in enumerate(extra_files):
                    # The index keeps two overlays with the same basename apart.
                    copy = Path(wrap_dir) / f"extra-{index:02d}-{extra.name}"
                    shutil.copyfile(extra, copy)
                    extras.append(copy)
                if pin_text is not None:
                    pin = Path(wrap_dir) / "avocado-pin.yml"
                    pin.write_text(pin_text)
                    extras.append(pin)
                out = _run_dump(kas_yaml, wrapper, extras, cwd=Path(wrap_dir), work_dir=work_dir)
        else:
            parent = wrap_parent.absolute()
            fd, name = tempfile.mkstemp(prefix=PROBE_PREFIX, suffix=".yml", dir=parent)
            wrapper = Path(name)
            pin_path: Path | None = None
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                extras = [extra.absolute() for extra in extra_files]
                if pin_text is not None:
                    pin_fd, pin_name = tempfile.mkstemp(prefix=PROBE_PREFIX + "pin-", suffix=".yml", dir=parent)
                    pin_path = Path(pin_name)
                    with os.fdopen(pin_fd, "w", encoding="utf-8") as fh:
                        fh.write(pin_text)
                    extras.append(pin_path)
                out = _run_dump(kas_yaml, wrapper, extras, cwd=parent, work_dir=work_dir)
            finally:
                wrapper.unlink(missing_ok=True)
                if pin_path is not None:
                    pin_path.unlink(missing_ok=True)

    return out, wrapper, work_dir_names


def _parse_dump(
    out: subprocess.CompletedProcess[str], kas_yaml: Path, wrapper: Path, work_dir_names: set[str], context: str
) -> dict:
    if out.returncode != 0:
        if _MISSING_MARKER in out.stderr:
            missing = _missing_repos(out.stderr) or "(kas did not name them)"
            raise ReleaseRoutingError(
                f"{kas_yaml} includes files from repositories other than its own checkout ({missing}); "
                "the release cannot be read without fetching them"
            )
        line = _first_error_line(out.stderr).replace(str(wrapper), str(kas_yaml))
        hint = _WORK_DIR_HINT if any(w in line for w in work_dir_names) else ""
        raise ReleaseRoutingError(f"kas dump of {kas_yaml}{context} failed: {line}{hint}")

    try:
        config = yaml.safe_load(out.stdout)
    except yaml.YAMLError as exc:
        raise ReleaseRoutingError(f"kas dump of {kas_yaml} printed unparseable YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ReleaseRoutingError(f"kas dump of {kas_yaml} did not print a mapping")
    return config


def _layer_foreign_repos(layer: bsp_detect.LayerRepo, export_dir: Path) -> list[str]:
    """Return every repository other than the layer repository and meta-avocado that the stack includes."""
    assert layer.entry is not None
    roots = {layer.name: layer.layer_root, _OWN_REPO: export_dir.resolve()}
    seen: set[Path] = set()
    foreign: list[str] = []
    pending = [layer.entry.resolve()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError, yaml.YAMLError:
            continue  # kas reads the same file and reports the problem in its own words
        header = data.get("header") if isinstance(data, dict) else None
        includes = header.get("includes") if isinstance(header, dict) else None
        files: list[Path] = []
        for inc in includes if isinstance(includes, list) else []:
            rel = inc
            root = path.parent
            if isinstance(inc, dict):
                repo = inc.get("repo")
                rel = inc.get("file")
                if repo is not None:
                    if str(repo) not in roots:
                        if str(repo) not in foreign:
                            foreign.append(str(repo))
                        continue
                    root = roots[str(repo)]
            if isinstance(rel, str):
                files.append((root / rel).resolve())
        pending += reversed(files)
    return foreign


def _git_read(clone: Path, *args: str, stdout: Path | None = None) -> tuple[int, str]:
    """Run a read-only git command in ``clone`` in its own session; return (exit code, stderr).

    Raises :class:`ReleaseRoutingError` naming the command and the clone when git
    does not finish in time.
    """
    with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as scratch:
        try:
            if stdout is None:
                proc = subprocess.run(
                    ["git", "-C", str(clone), *args],
                    stdout=scratch,
                    stderr=err,
                    timeout=_GIT_READ_TIMEOUT_S,
                    check=False,
                    start_new_session=True,
                )
            else:
                with stdout.open("wb") as out:
                    proc = subprocess.run(
                        ["git", "-C", str(clone), *args],
                        stdout=out,
                        stderr=err,
                        timeout=_GIT_READ_TIMEOUT_S,
                        check=False,
                        start_new_session=True,
                    )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseRoutingError(
                f"git {args[0]} in {clone} did not finish within {_GIT_READ_TIMEOUT_S}s; "
                "the meta-avocado clone could not be read"
            ) from exc
        err.seek(0)
        return proc.returncode, err.read().decode(errors="replace").strip()


def _export_dependency(layer: bsp_detect.LayerRepo, clone: Path, export_dir: Path) -> None:
    """Export the dependency's ``kas`` tree at the entry's pinned commit into ``export_dir``.

    Only reads objects the clone already holds (``cat-file``, ``archive``): HEAD,
    index, refs and working tree are never touched, and nothing is fetched.
    """
    commit = layer.dependency.get("commit")
    if not commit:
        raise ReleaseRoutingError(
            f"{layer.entry} declares {_OWN_REPO} without a commit, so its kas files cannot be read from a pin"
        )
    if not clone.is_dir():
        raise ReleaseRoutingError(
            f"the workspace has no {_OWN_REPO} clone at {clone}, so the pin {commit} of {_OWN_REPO} cannot be read; "
            "bakar does not clone into a shared workspace"
        )
    if _git_read(clone, "cat-file", "-e", f"{commit}^{{commit}}")[0] != 0:
        raise ReleaseRoutingError(
            f"commit {commit} of {_OWN_REPO} is not in {clone}; bakar does not fetch into a shared clone - "
            f"run: git -C {clone} fetch origin {commit}"
        )
    tar_path = export_dir.parent / "kas-export.tar"
    code, detail = _git_read(clone, "archive", "--format=tar", str(commit), "kas", stdout=tar_path)
    if code != 0:
        raise ReleaseRoutingError(
            f"cannot export the kas files of commit {commit} from {clone}: {detail or '(no stderr)'}"
        )
    export_dir.mkdir()
    with tarfile.open(tar_path) as tar:
        tar.extractall(export_dir, filter="data")


def read_merged_config_layer(
    layer: bsp_detect.LayerRepo,
    clone: Path,
    *,
    extra_files: Sequence[Path] = (),
    wrap_parent: Path | None = None,
) -> dict:
    """Return the merged kas config of a layer entry, its dependency read from the entry's pinned commit.

    ``clone`` is the workspace's meta-avocado clone. Its ``kas`` tree at the
    pinned commit is exported with ``git archive`` into a throwaway directory
    and declared there, without a url, in the probe wrapper; the layer
    repository is declared by its absolute path only. The clone is only read.

    Raises ReleaseRoutingError, before kas runs, when the pin is absent from
    ``clone`` (naming the commit, the clone and the fetch command) or when an
    include in the stack names a repository other than the layer repository
    and meta-avocado.
    """
    if layer.entry is None:
        raise ValueError("the layer repository carries no entry file")
    with tempfile.TemporaryDirectory(prefix="bakar-kas-layer-export-") as tmp:
        export_dir = Path(tmp) / "meta-avocado"
        _export_dependency(layer, clone, export_dir)
        foreign = _layer_foreign_repos(layer, export_dir)
        if foreign:
            raise ReleaseRoutingError(
                f"{layer.entry} includes files from repositories other than {layer.name} and {_OWN_REPO} "
                f"({', '.join(foreign)}); the release cannot be read without fetching them"
            )
        text = bsp_detect.layer_probe_wrapper_text(layer, export_dir)
        pin_text = bsp_detect.layer_pin_text(layer) if extra_files else None
        out, wrapper, work_dir_names = _dump_wrapped(layer.entry, text, pin_text, extra_files, wrap_parent)
    context = ""
    if out.returncode != 0:
        context = _failure_context(layer.checkout, extra_files)
    return _parse_dump(out, layer.entry, wrapper, work_dir_names, context)


def probe_pins(kas_yaml: Path, checkout: Path) -> tuple[str, dict]:
    """Return ``(release key, pinned repos)`` from a single kas dump of ``kas_yaml``."""
    merged = read_merged_config(kas_yaml, checkout=checkout)
    return release_key_from_merged(merged), merged.get("repos") or {}


def probe_release(kas_yaml: Path, checkout: Path) -> str:
    """Return the release key (``wrynose``) pinned by ``kas_yaml``."""
    return probe_pins(kas_yaml, checkout)[0]

"""Unit tests for bakar.bsp_detect.layer_repo_of.

A layer entry is a kas file in a git checkout (not meta-avocado) that declares
exactly one repository without a url and meta-avocado with a url. The layer
repository's name comes from the declaration, never from the directory.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

import bakar.bsp_detect
from bakar.bsp_detect import LayerRepo, layer_repo_of
from bakar.release_workspace import ReleaseRoutingError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

_ENTRY = """\
header:
  version: 16
repos:
  meta-board:
    path: .
  meta-avocado:
    url: https://example.invalid/peridio/meta-avocado.git
    branch: wrynose
    commit: 97619b8d89b42105fdf6dd83ecec628453b615cb
"""


def _git(*args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    ident = ["-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", *ident, *args], check=True, capture_output=True, env=env)


def _repo(directory: Path, origin: str | None = "https://example.invalid/peridio/meta-board.git") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", str(directory))
    if origin is not None:
        _git("-C", str(directory), "remote", "add", "origin", origin)
    _git("-C", str(directory), "commit", "-q", "--allow-empty", "-m", "init")
    return directory


def _entry(checkout: Path, body: str = _ENTRY, rel: str = "kas/machine/m.yml") -> Path:
    kas = checkout / rel
    kas.parent.mkdir(parents=True, exist_ok=True)
    kas.write_text(body, encoding="utf-8")
    return kas


def test_main_checkout_is_a_layer_entry(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "ws" / "meta-board")
    found = layer_repo_of(_entry(checkout))
    assert found is not None
    assert found.checkout == checkout.resolve()
    assert found.name == "meta-board"
    assert found.dependency == {
        "url": "https://example.invalid/peridio/meta-avocado.git",
        "branch": "wrynose",
        "commit": "97619b8d89b42105fdf6dd83ecec628453b615cb",
    }
    assert found.is_linked_worktree is False
    assert found.checkout_name == "meta-board"
    assert found.branch == "main"


def test_layer_repository_declared_by_its_own_name_is_a_layer_entry(tmp_path: Path) -> None:
    # the shape of a real layer entry: kas places a path: repository relative to the
    # build directory's parent, so the layer repository is named by the layer
    # directory inside the checkout (path: meta-board, a subdirectory)
    body = _ENTRY.replace("    path: .\n", "    path: meta-board\n")
    checkout = _repo(tmp_path / "ws" / "meta-board")
    (checkout / "meta-board" / "conf").mkdir(parents=True)
    found = layer_repo_of(_entry(checkout, body))
    assert found is not None
    assert found.name == "meta-board"


def test_layer_repo_is_frozen(tmp_path: Path) -> None:
    found = layer_repo_of(_entry(_repo(tmp_path / "meta-board")))
    assert isinstance(found, LayerRepo)
    with pytest.raises(AttributeError):
        found.name = "x"  # type: ignore[misc]


def test_name_comes_from_declaration_not_directory(tmp_path: Path) -> None:
    main = _repo(tmp_path / "meta-board")
    wt = tmp_path / "wts" / "feature-x"
    wt.parent.mkdir()
    _git("-C", str(main), "worktree", "add", "-q", str(wt), "-b", "feature/x")
    found = layer_repo_of(_entry(wt))
    assert found is not None
    assert found.name == "meta-board"
    assert found.checkout_name == "feature-x"
    assert found.checkout == wt.resolve()
    assert found.is_linked_worktree is True
    assert found.branch == "feature/x"


def test_detached_head_has_no_branch(tmp_path: Path) -> None:
    main = _repo(tmp_path / "meta-board")
    _git("-C", str(main), "checkout", "-q", "--detach")
    found = layer_repo_of(_entry(main))
    assert found is not None
    assert found.branch is None


def test_worktree_of_bare_repository_is_resolved(tmp_path: Path) -> None:
    bare = tmp_path / "srv" / "board.git"
    bare.parent.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("-C", str(bare), "remote", "add", "origin", "https://example.invalid/peridio/meta-board.git")
    seed = _repo(tmp_path / "seed", None)
    _git("-C", str(seed), "push", "-q", str(bare), "main")
    wt = tmp_path / "wts" / "zedx"
    wt.parent.mkdir(parents=True)
    _git("-C", str(bare), "worktree", "add", "-q", str(wt), "main")
    found = layer_repo_of(_entry(wt))
    assert found is not None
    assert found.checkout == wt.resolve()
    assert found.checkout_name == "zedx"
    assert found.name == "meta-board"
    assert found.is_linked_worktree is True
    assert found.branch == "main"


def test_inside_meta_avocado_checkout_is_none(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "ws" / "meta-avocado", "https://example.invalid/peridio/meta-avocado.git")
    assert layer_repo_of(_entry(checkout)) is None


def test_inside_meta_avocado_worktree_is_none(tmp_path: Path) -> None:
    main = _repo(tmp_path / "ws" / "meta-avocado", "https://example.invalid/peridio/meta-avocado.git")
    wt = tmp_path / "elsewhere" / "wt"
    wt.parent.mkdir()
    _git("-C", str(main), "worktree", "add", "-q", str(wt), "-b", "wt")
    assert layer_repo_of(_entry(wt)) is None


def test_meta_avocado_clone_under_other_directory_name_is_none(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "clone", "git@example.invalid:peridio/meta-avocado.git")
    assert layer_repo_of(_entry(checkout)) is None


def test_no_url_less_repository_is_none(tmp_path: Path) -> None:
    body = _ENTRY.replace("    path: .\n", "    url: https://example.invalid/x.git\n")
    assert layer_repo_of(_entry(_repo(tmp_path / "meta-board"), body)) is None


def test_no_meta_avocado_dependency_is_none(tmp_path: Path) -> None:
    body = "header:\n  version: 16\nrepos:\n  meta-board:\n    path: .\n"
    assert layer_repo_of(_entry(_repo(tmp_path / "meta-board"), body)) is None


def test_meta_avocado_without_url_is_not_a_layer_entry(tmp_path: Path) -> None:
    body = "repos:\n  meta-board:\n    path: .\n  meta-avocado:\n    path: meta-avocado\n"
    # no dependency url: the file is a generic one, never a layer entry and never an error
    assert layer_repo_of(_entry(_repo(tmp_path / "meta-board"), body)) is None


def test_generic_file_with_two_url_less_repositories_is_none(tmp_path: Path) -> None:
    # bsp_root and triage call layer_repo_of for every generic file, so a file that
    # does not depend on meta-avocado must not raise however many local repos it has
    body = "repos:\n  meta-a:\n    path: .\n  meta-b:\n    path: b\n"
    assert layer_repo_of(_entry(_repo(tmp_path / "meta-a"), body)) is None


def test_outside_any_repository_is_none(tmp_path: Path) -> None:
    assert layer_repo_of(_entry(tmp_path / "plain")) is None


def test_two_url_less_repositories_raise_naming_both(tmp_path: Path) -> None:
    # Both entries resolve to the checkout root (a path of ``.`` and no path at all).
    body = _ENTRY.replace("  meta-avocado:", "  meta-other: {}\n  meta-avocado:", 1)
    kas = _entry(_repo(tmp_path / "meta-board"), body)
    with pytest.raises(ReleaseRoutingError) as exc:
        layer_repo_of(kas)
    assert "meta-board" in str(exc.value)
    assert "meta-other" in str(exc.value)
    assert layer_repo_of(kas, strict=False) is None


def test_url_less_entry_with_a_path_elsewhere_is_a_local_layer_not_the_layer_repository(tmp_path: Path) -> None:
    body = _ENTRY.replace("  meta-avocado:", "  local-layer:\n    path: ../x\n  meta-avocado:", 1)
    found = layer_repo_of(_entry(_repo(tmp_path / "meta-board"), body))
    assert found is not None
    assert found.name == "meta-board"


def test_only_a_path_elsewhere_entry_is_not_a_layer_entry(tmp_path: Path) -> None:
    body = _ENTRY.replace("  meta-board:\n    path: .\n", "  local-layer:\n    path: ../x\n", 1)
    assert layer_repo_of(_entry(_repo(tmp_path / "meta-board"), body)) is None


def test_a_missing_file_is_not_an_entry(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "meta-board")
    assert layer_repo_of(checkout / "kas" / "absent.yml") is None
    assert layer_repo_of(checkout / "kas") is None


def test_non_strict_turns_a_yaml_error_into_none(tmp_path: Path) -> None:
    kas = _entry(_repo(tmp_path / "meta-board"), "repos: [unclosed\n  - : :\n")
    assert layer_repo_of(kas, strict=False) is None
    with pytest.raises(ReleaseRoutingError):
        layer_repo_of(kas)


def test_invalid_yaml_raises_naming_the_file(tmp_path: Path) -> None:
    kas = _entry(_repo(tmp_path / "meta-board"), "repos: [unclosed\n  - : :\n")
    with pytest.raises(ReleaseRoutingError) as exc:
        layer_repo_of(kas)
    assert str(kas) in str(exc.value)
    assert "could not be read" in str(exc.value)


def test_repos_declared_through_a_string_include(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "meta-board")
    kas = _entry(
        checkout,
        "header:\n  version: 16\n  includes:\n    - base.yml\nrepos:\n  meta-avocado:\n    commit: abc123\n",
        "kas/machine/pinned.yml",
    )
    # include path is relative to the including file
    (checkout / "kas" / "machine" / "base.yml").write_text(_ENTRY, encoding="utf-8")
    found = layer_repo_of(kas)
    assert found is not None
    assert found.name == "meta-board"
    assert found.dependency["commit"] == "abc123"
    assert found.dependency["url"] == "https://example.invalid/peridio/meta-avocado.git"
    assert found.dependency["branch"] == "wrynose"


def test_include_escaping_the_checkout_is_not_read(tmp_path: Path) -> None:
    checkout = _repo(tmp_path / "meta-board")
    (tmp_path / "outside.yml").write_text(_ENTRY, encoding="utf-8")
    kas = _entry(checkout, "header:\n  includes:\n    - ../../outside.yml\n", "kas/m.yml")
    assert layer_repo_of(kas) is None


def test_runs_no_kas_and_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kas = _entry(_repo(tmp_path / "meta-board"))
    seen: list[str] = []
    real = bakar.bsp_detect.git_stdout

    def spy(repo: Path, *args: str, **kw: object) -> str | None:
        seen.append(args[0])
        return real(repo, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(bakar.bsp_detect, "git_stdout", spy)
    assert layer_repo_of(kas) is not None
    assert set(seen) <= {"config"}


def _generic_cfg(workspace: Path, kas_yaml: Path):
    from bakar.config import BuildConfig

    return BuildConfig(
        workspace=workspace,
        bsp_family="generic",  # type: ignore[arg-type]
        machine="generic",
        distro="generic",
        image="generic",
        manifest="",
        repo_url="",
        repo_branch="",
        kas_container_image="jetm/kas-build-env:latest",
        kas_yaml_override=kas_yaml,
    )


def test_bsp_root_of_a_generic_file_with_a_yaml_error_is_the_files_directory(tmp_path: Path) -> None:
    kas = _entry(_repo(tmp_path / "meta-board"), "repos: [unclosed\n  - : :\n")
    assert _generic_cfg(tmp_path / "ws", kas).bsp_root == kas.parent


def test_bsp_root_of_a_missing_generic_file_is_the_files_directory(tmp_path: Path) -> None:
    kas = tmp_path / "meta-board" / "kas" / "absent.yml"
    assert _generic_cfg(tmp_path / "ws", kas).bsp_root == kas.parent


def test_triage_reaches_its_generic_branch_on_a_mid_edit_file(tmp_path: Path) -> None:
    from bakar.commands.triage import _resolve_triage_dirs

    kas = _entry(_repo(tmp_path / "meta-board"), "repos: [unclosed\n  - : :\n")
    runs_dirs, report_root, _label, translation = _resolve_triage_dirs(kas, None)
    assert report_root == kas.resolve().parent
    assert translation == report_root
    assert runs_dirs[0][0] == report_root / "build" / "runs"


@pytest.mark.parametrize("name", ["on", "yes", "null", "a:b", "a #b", "1", "meta board"])
def test_layer_repository_name_is_quoted_when_yaml_would_misread_it(tmp_path: Path, name: str) -> None:
    import yaml

    checkout = tmp_path / "co"
    layer = LayerRepo(
        checkout=checkout,
        name=name,
        dependency={},
        is_linked_worktree=False,
        checkout_name="co",
        branch="main",
        entry=checkout / "kas" / "e.yml",
    )
    export = tmp_path / "export"

    for text in (
        bakar.bsp_detect.layer_pin_text(layer),
        bakar.bsp_detect.layer_wrapper_text(layer, export),
        bakar.bsp_detect.layer_probe_wrapper_text(layer, export),
    ):
        doc = yaml.safe_load(text)
        assert name in doc["repos"], text
        assert set(doc["repos"]) <= {name, "meta-avocado"}
    wrapper = yaml.safe_load(bakar.bsp_detect.layer_wrapper_text(layer, export))
    assert wrapper["header"]["includes"][0]["repo"] == name


def test_ordinary_layer_repository_name_stays_unquoted(tmp_path: Path) -> None:
    checkout = tmp_path / "co"
    layer = LayerRepo(checkout, "meta-board", {}, False, "co", "main", checkout / "kas" / "e.yml")

    assert "\n  meta-board:\n" in bakar.bsp_detect.layer_pin_text(layer)

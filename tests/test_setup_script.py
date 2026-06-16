"""Tests for the privileged-script renderer.

Covers that :func:`render_script` begins with ``set -euo pipefail``, emits only
``needs_root`` operations (an unprivileged op's command never appears), shell-
quotes a ``RunCommand`` faithfully (the embedded ``python3 -c`` daemon.json
merge survives), renders a ``WriteFile`` as a guarded ``cp`` backup plus a
quoted heredoc, and never emits a ``curl``-piped-to-shell line.
"""

from __future__ import annotations

import shlex

from bakar.setup import script as script_mod
from bakar.setup.actions.base import RunCommand, WriteFile
from bakar.setup.script import render_script


def test_script_starts_with_shebang_and_strict_mode() -> None:
    """The rendered script's first lines are the shebang then strict mode."""
    text = render_script([RunCommand(argv=["sysctl", "--system"], needs_root=True)])
    lines = text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines[1] == "set -euo pipefail"


def test_only_privileged_operations_are_rendered() -> None:
    """An unprivileged operation's command never appears in the script."""
    ops: list[RunCommand | WriteFile] = [
        RunCommand(argv=["usermod", "-aG", "docker", "alice"], needs_root=True),
        RunCommand(argv=["uv", "tool", "install", "kas"], needs_root=False),
    ]
    text = render_script(ops)
    assert "usermod" in text
    assert "uv tool install kas" not in text
    assert "kas" not in text


def test_run_command_is_shell_quoted() -> None:
    """Each argv element is shlex-quoted, preserving an embedded script arg.

    A multi-line script with quotes and newlines round-trips back to the exact
    original argv when re-parsed with ``shlex.split`` - proving the quoting is
    faithful, not a lossy substring.
    """
    merge_script = "import json\ndata = {'a': 1}\nprint(data[\"a\"])\n"
    op = RunCommand(argv=["python3", "-c", merge_script], needs_root=True)
    text = render_script([op])
    assert "python3 -c " in text
    # The rendered command (everything after the strict-mode header and its
    # trailing blank line) re-parses back into the original argv. shlex.split
    # consumes the embedded newlines inside the single-quoted argument.
    header = "#!/usr/bin/env bash\nset -euo pipefail\n\n"
    command = text[len(header) :]
    assert shlex.split(command) == ["python3", "-c", merge_script]


def test_write_file_with_backup_renders_guarded_cp_and_heredoc() -> None:
    """A backup WriteFile emits an existence-guarded cp then a quoted heredoc."""
    op = WriteFile(
        path="/etc/docker/daemon.json",
        content='{\n  "storage-driver": "overlay2"\n}\n',
        needs_root=True,
        backup=True,
    )
    text = render_script([op])
    assert "if [ -e /etc/docker/daemon.json ]; then cp /etc/docker/daemon.json /etc/docker/daemon.json.bak; fi" in text
    assert "cat > /etc/docker/daemon.json <<'BAKAR_EOF'" in text
    assert '"storage-driver": "overlay2"' in text
    assert "\nBAKAR_EOF\n" in text


def test_write_file_without_backup_skips_cp() -> None:
    """A WriteFile with backup=False emits no cp line."""
    op = WriteFile(
        path="/etc/sysctl.d/99-bakar.conf",
        content="vm.swappiness = 10\n",
        needs_root=True,
        backup=False,
    )
    text = render_script([op])
    assert "cp " not in text
    assert "cat > /etc/sysctl.d/99-bakar.conf <<'BAKAR_EOF'" in text


def test_no_curl_piped_to_shell() -> None:
    """The renderer never emits a curl-to-shell pipe."""
    ops: list[RunCommand | WriteFile] = [
        RunCommand(argv=["systemctl", "enable", "--now", "docker"], needs_root=True),
        WriteFile(path="/etc/sysctl.d/99-bakar.conf", content="x = 1\n", needs_root=True, backup=False),
    ]
    text = render_script(ops)
    assert "curl" not in text


def test_script_module_imported_via_package() -> None:
    """The renderer is reachable from the setup package namespace."""
    assert hasattr(script_mod, "render_script")

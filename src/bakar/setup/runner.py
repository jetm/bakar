"""Apply a :class:`~bakar.setup.plan.SetupPlan` to the host.

:func:`apply_plan` is the orchestration layer ``bakar setup`` delegates to once
the plan is built and shown. It enforces the change's escalation model:

- **One sudo, ever.** Every privileged operation across all actions is collected
  and rendered into a single ``bakar-host-setup.sh`` (see
  :func:`bakar.setup.script.write_script`). That script runs under exactly one
  ``sudo bash <path>`` - never a per-action ``sudo``.
- **Confirm before escalating (interactive).** When privileged operations exist
  and ``assume_yes`` is False, the user is asked to confirm; declining applies
  *nothing* (no privileged script, no unprivileged op, no config persist) and
  returns without error.
- **Passwordless precheck (non-interactive).** With ``assume_yes`` True the
  runner runs ``sudo -n true`` first; if passwordless sudo is unavailable it
  exits non-zero with a clear message rather than blocking on a password prompt.

Unprivileged operations (``needs_root=False`` :class:`RunCommand` / :class:`WriteFile`)
run inline in the current user context - plain ``subprocess.run`` / file write,
no ``sudo``. The :class:`~bakar.setup.actions.config_write.ConfigWriteAction`
(which yields no operations and persists via its ``apply()`` method) is invoked
last, only when the run was not declined, so the recorded ``[host]`` values
reflect a successful apply.

The bare ``subprocess.run`` / ``sudo`` invocation lines are ``# pragma: no
cover`` per recap ``test-steps-executors`` (running real ``sudo`` / ``systemctl``
under test is not feasible); the surrounding routing logic - the confirm-decline
path, the ``sudo -n`` precheck branch, and the privileged-vs-unprivileged split -
is exercised by monkeypatching ``subprocess.run``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from bakar.commands._app import console
from bakar.setup.script import write_script

if TYPE_CHECKING:
    from bakar.setup.actions.base import Action, RunCommand, WriteFile
    from bakar.setup.plan import SetupPlan


def _has_passwordless_sudo() -> bool:
    """Whether ``sudo`` can run without prompting for a password.

    Runs ``sudo -n true``: ``-n`` makes sudo fail immediately instead of
    prompting, so this never blocks. Returns True only on a clean exit.
    """
    result = subprocess.run(  # pragma: no cover - bare sudo invocation
        ["sudo", "-n", "true"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _run_unprivileged(op: RunCommand | WriteFile) -> None:
    """Apply one unprivileged operation inline in the user context.

    A :class:`RunCommand` is executed via ``subprocess.run``; a
    :class:`WriteFile` writes its content directly (with an optional backup of a
    pre-existing target). Neither path uses ``sudo`` - these operations only ever
    touch user-owned paths.
    """
    if hasattr(op, "argv"):
        subprocess.run(op.argv, check=True)  # pragma: no cover - bare subprocess invocation
        return
    target = Path(op.path)
    if op.backup and target.exists():
        target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    content = op.content if op.content.endswith("\n") else op.content + "\n"
    target.write_text(content, encoding="utf-8")


def _run_privileged_script(operations: list[RunCommand | WriteFile]) -> None:
    """Render the privileged operations and run them under one ``sudo bash``."""
    path = write_script(operations)
    subprocess.run(["sudo", "bash", str(path)], check=True)  # pragma: no cover - bare sudo invocation


def apply_plan(plan: SetupPlan, *, assume_yes: bool = False) -> None:
    """Apply ``plan`` to the host, escalating privileges exactly once.

    Order:

    1. Collect every ``needs_root`` operation. If any exist, gate the escalation:
       interactive mode confirms (declining returns without applying anything);
       ``assume_yes`` mode runs a ``sudo -n true`` precheck and exits non-zero
       when passwordless sudo is unavailable. Then run the single privileged
       script.
    2. Run every unprivileged operation inline.
    3. Persist applied host knobs via the config-write action's ``apply()``,
       only when step 1 was not declined.

    Raises :class:`typer.Exit` with a non-zero code when ``assume_yes`` is set
    but passwordless sudo is not available.
    """
    privileged_ops: list[RunCommand | WriteFile] = []
    for action in plan.actions:
        privileged_ops.extend(op for op in action.operations() if op.needs_root)

    if privileged_ops:
        if assume_yes:
            if not _has_passwordless_sudo():
                console.print(
                    "[red]Passwordless sudo unavailable:[/] --yes needs `sudo -n true` to "
                    "succeed so the privileged setup script can run without prompting. "
                    "Re-run without --yes to confirm interactively, or configure passwordless sudo."
                )
                raise typer.Exit(code=1)
        else:
            confirmed = typer.confirm(
                f"Apply {len(privileged_ops)} privileged operation(s) via one sudo script?"
            )
            if not confirmed:
                console.print("Declined - no changes made.")
                return
        _run_privileged_script(privileged_ops)

    for action in plan.actions:
        for op in action.operations():
            if not op.needs_root:
                _run_unprivileged(op)

    for action in plan.actions:
        _apply_config_write(action)


def _apply_config_write(action: Action) -> None:
    """Invoke a config-write action's ``apply()`` to persist host knobs.

    The :class:`~bakar.setup.actions.config_write.ConfigWriteAction` yields no
    shell operations - it persists directly. It is detected by the presence of an
    ``apply`` method so the runner need not import the concrete class.
    """
    apply = getattr(action, "apply", None)
    if callable(apply):
        apply()

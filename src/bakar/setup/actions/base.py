"""The :class:`Action` protocol and operation primitives for ``bakar setup``.

An :class:`Action` is the unit the plan builder maps a host-environment
``doctor`` check to: it carries the ``check_name`` it remediates, declares
whether it ``needs_root``, decides whether the live :class:`HostProfile`
already satisfies its recommended target, and yields the operation primitives
that apply it. The two primitives - :class:`RunCommand` and :class:`WriteFile`
- are the only shapes the script renderer and the runner understand.

Interface contract for downstream action modules: an action owns the
recommended target value(s) it applies as constants (never config reads);
``is_satisfied(profile)`` compares the live value carried on the
:class:`HostProfile` against that recommended target; an action that needs a
resolved-config value (e.g. the container image) receives it as a constructor
argument from the plan builder, never by calling ``resolve()`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bakar.setup.profile import HostProfile


@dataclass(frozen=True)
class RunCommand:
    """A command to execute as one operation of an :class:`Action`.

    ``needs_root`` distinguishes a privileged op (rendered into the single
    ``sudo`` script) from an unprivileged op (run inline in the user context).
    """

    argv: list[str]
    needs_root: bool


@dataclass(frozen=True)
class WriteFile:
    """A file-write operation of an :class:`Action`.

    ``backup`` requests a copy of any pre-existing file before the write (e.g.
    ``daemon.json.bakar.bak``) so a bad merge can be reverted. ``needs_root``
    distinguishes a privileged write (a system path) from an unprivileged one.
    """

    path: str
    content: str
    needs_root: bool
    backup: bool


@runtime_checkable
class Action(Protocol):
    """A single remediation for one host-environment ``doctor`` check.

    ``check_name`` is the ``CheckResult.name`` this action remediates; it links
    a check to its fix and lets a follow-up ``doctor`` confirm the named check
    flips to PASS. ``needs_root`` declares whether any of the action's
    operations require the single ``sudo`` escalation.
    """

    check_name: str
    needs_root: bool

    def describe(self) -> str:
        """A one-line human summary of what applying this action does."""
        ...

    def is_satisfied(self, profile: HostProfile) -> bool:
        """Whether ``profile`` already meets this action's recommended target.

        When True the plan builder drops the action, so a prepared host yields
        an empty plan.
        """
        ...

    def operations(self) -> list[RunCommand | WriteFile]:
        """The ordered operation primitives that apply this action."""
        ...

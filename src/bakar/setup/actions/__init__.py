"""Per-domain remediation actions for ``bakar setup``.

Each module here defines one or more :class:`~bakar.setup.actions.base.Action`
implementations that remediate a single host-environment ``doctor`` check. An
action owns its recommended target value(s) as constants, decides whether the
live :class:`~bakar.setup.profile.HostProfile` already satisfies them, and
yields the :class:`~bakar.setup.actions.base.RunCommand` /
:class:`~bakar.setup.actions.base.WriteFile` primitives the plan and script
renderer turn into work.
"""

from __future__ import annotations

from bakar.setup.actions.base import Action, RunCommand, WriteFile

__all__ = ["Action", "RunCommand", "WriteFile"]

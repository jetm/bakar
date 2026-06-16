"""Once-per-machine host preparation for ``bakar setup``.

Profiles the host (CPU, RAM, disk, distro, package manager, docker
group/binary, and the live sysctl/ulimit knobs), maps the host-environment
``doctor`` checks to remediation actions, and applies them under a single
auditable ``sudo`` escalation. See ``profile.py`` for the read-only host
profiler that the plan builder and per-action ``is_satisfied`` checks
compare against.
"""

from __future__ import annotations

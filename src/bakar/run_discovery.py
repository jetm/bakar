"""Workspace-wide run discovery.

Finds the build runs under a workspace and decides which are live. It imports
``bakar.build_stop`` and ``bakar.stop_container`` as modules, never by
from-import, so that patches of the stop module's names keep intercepting
discovery at call time.
"""

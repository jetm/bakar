"""``python -m bakar`` goes through ``main()`` like the ``bakar`` script does."""

from __future__ import annotations

import runpy

import pytest

import bakar.cli

pytestmark = pytest.mark.unit


def test_python_dash_m_bakar_exits_with_main_return_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bakar.cli, "main", lambda: 7)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bakar", run_name="__main__")

    assert excinfo.value.code == 7

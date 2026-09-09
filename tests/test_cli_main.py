"""Coverage for the bakar entry-point error interceptor (cli.main).

These tests invoke ``bakar.cli.main`` directly with ``sys.argv``
monkeypatched, so the actual entry-point path is exercised end-to-end
(no ``CliRunner``). Each test asserts both the return code and the
absence of Rich box-drawing characters in stderr, proving the
interceptor short-circuited before Typer's rich_utils panel formatter
could render.
"""

from __future__ import annotations

import sys

import pytest

from bakar.cli import main

pytestmark = pytest.mark.unit


def test_main_unknown_option_returns_exit_code_2_and_no_panel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo'd option produces plain stderr - no Rich box-drawing characters."""
    monkeypatch.setattr(sys, "argv", ["bakar", "--no-such-option"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert "Error:" in captured.err
    # The box character `╭` is what Typer's Rich panel formatter emits. Its absence
    # proves the interceptor short-circuited before rich_utils could render the panel.
    assert "╭" not in captured.err
    assert "╰" not in captured.err


def test_main_unexpected_extra_argument_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two extra positionals after ``hashserv status kas.yml`` trip UsageError."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["bakar", "hashserv", "status", "kas.yml", "second-extra"],
    )
    rc = main()
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert "Error:" in captured.err
    assert "╭" not in captured.err
    assert "╰" not in captured.err


def test_main_returns_0_on_normal_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bakar --help`` exits cleanly via the typer.Exit branch."""
    monkeypatch.setattr(sys, "argv", ["bakar", "--help"])
    rc = main()
    assert rc == 0


def test_main_returns_exit_code_from_typer_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``settings get`` on an unknown key raises typer.Exit(2); interceptor surfaces 2."""
    monkeypatch.setattr(sys, "argv", ["bakar", "settings", "get", "no.such.key"])
    rc = main()
    assert rc == 2


def test_main_buildtools_missing_returns_1_clean(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A BuildtoolsMissingError (host-mode inspection on a stock host) surfaces as a
    plain 'Error:' with rc 1, not a raw traceback."""
    import bakar.cli as cli_mod
    from bakar.steps.kas_build import BuildtoolsMissingError

    def _raise(**_kw: object) -> int:
        raise BuildtoolsMissingError("buildtools-extended toolchain not found; set BAKAR_BUILDTOOLS_DIR")

    monkeypatch.setattr(cli_mod, "app", _raise)
    monkeypatch.setattr(sys, "argv", ["bakar", "getvar", "FOO"])
    rc = main()
    captured = capsys.readouterr()
    assert rc == 1, captured.err
    assert "Error:" in captured.err
    assert "buildtools-extended" in captured.err
    assert "╭" not in captured.err


class TestGlobalCallbackPublishesModuleState:
    """The ``@app.callback()`` shim must still publish its flags as module globals.

    Readers resolve them as attributes on ``bakar.commands._app`` at call time
    (``_state._SCCACHE_DIST`` in commands/build.py, ``global_host_mode()`` and
    friends in commands/_helpers.py), so a context object that carries the values
    without assigning them back leaves every reader on its import-time default.
    """

    _GLOBALS = (
        "_HIDE_DOCTOR_REPORT",
        "_HOST_MODE",
        "_CONTAINER_MODE",
        "_NO_SCOPE",
        "_SCCACHE_DIST",
        "_SCCACHE_SCHEDULER",
        "_MOLD",
        "_MOLD_BASELINE",
        "_MOLD_GLOBAL",
        "_OUTPUT_MODE_OVERRIDE",
    )

    @pytest.fixture(autouse=True)
    def _restore(self):
        import bakar.commands._app as state

        saved = {name: getattr(state, name) for name in self._GLOBALS}
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(state, name, value)

    @staticmethod
    def _invoke(*args: str):
        from typer.testing import CliRunner

        import bakar.cli  # noqa: F401 - registers every subcommand on the shared app
        import bakar.commands._app as state

        result = CliRunner().invoke(state.app, [*args, "doctor", "--help"])
        assert result.exit_code == 0, result.output
        return state

    def test_sccache_flags_land_on_module_globals(self) -> None:
        state = self._invoke("--sccache-dist", "--sccache-scheduler", "http://localhost:10600")
        assert state._SCCACHE_DIST is True
        assert state._SCCACHE_SCHEDULER == "http://localhost:10600"

    def test_host_mode_lands_on_module_global(self) -> None:
        from bakar.commands._helpers import global_host_mode

        state = self._invoke("--host")
        assert state._HOST_MODE is True
        assert global_host_mode() is True

    def test_container_mode_lands_on_module_global(self) -> None:
        from bakar.commands._helpers import global_container_mode

        state = self._invoke("--container")
        assert state._CONTAINER_MODE is True
        assert global_container_mode() is True

    def test_output_mode_override_lands_on_module_global(self) -> None:
        from bakar.output_mode import OutputMode

        state = self._invoke("--plain")
        assert state._OUTPUT_MODE_OVERRIDE is OutputMode.PLAIN

    def test_remaining_globals_land_on_module_globals(self) -> None:
        state = self._invoke("--hide-doctor-report", "--no-scope", "--mold")
        assert state._HIDE_DOCTOR_REPORT is True
        assert state._NO_SCOPE is True
        assert state._MOLD is True

    @pytest.mark.parametrize(
        ("flag", "name"),
        [
            ("--hide-doctor-report", "_HIDE_DOCTOR_REPORT"),
            ("--no-scope", "_NO_SCOPE"),
            ("--mold", "_MOLD"),
            ("--mold-baseline", "_MOLD_BASELINE"),
            ("--mold-global", "_MOLD_GLOBAL"),
        ],
    )
    def test_one_boolean_global_at_a_time(self, flag: str, name: str) -> None:
        """Exactly one flag set, so a transposition between two of them fails.

        The test above passes three flags together and asserts all three are
        True, which reads the same whichever way two of them are swapped. It
        also never touched ``_MOLD_BASELINE`` or ``_MOLD_GLOBAL`` at all, even
        though both appear in this class's save/restore tuple - so writing
        ``_MOLD_BASELINE = opts.mold_global`` passed the whole suite while
        ``bakar --mold-global`` selected the bfd-baseline arm of
        ``apply_mold_overrides`` instead of the global-mold arm.
        """
        others = ["_HIDE_DOCTOR_REPORT", "_NO_SCOPE", "_MOLD", "_MOLD_BASELINE", "_MOLD_GLOBAL"]
        state = self._invoke(flag)

        for other in others:
            want = other == name
            assert getattr(state, other) is want, (
                f"passing {flag} should set only {name}: {other} is {getattr(state, other)!r}"
            )

    def test_startup_hooks_still_populate_presets_and_vendors(self) -> None:
        state = self._invoke()
        assert state._PRESETS is not None
        assert state._VENDORS is not None
        assert state._USER_CONFIG is not None

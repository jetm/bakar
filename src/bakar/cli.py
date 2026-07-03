"""bakar entry point - imports all command modules to register @app.command() handlers."""

from __future__ import annotations

import sys

import typer

# Typer >= 0.26 vendored Click as ``typer._click``; the exceptions raised
# inside Typer's parser are ``typer._click.exceptions.*``, not the external
# ``click.exceptions.*``. Older Typer still raises from the external module.
# Catch from both so the entry point works regardless of which Typer ships.
try:
    from typer._click import exceptions as _click_exc  # ty: ignore[unresolved-import]
except ImportError:  # pragma: no cover - typer < 0.26 path
    from click import exceptions as _click_exc

import bakar.commands.bitbake
import bakar.commands.build
import bakar.commands.changelog
import bakar.commands.clean
import bakar.commands.clean_cache
import bakar.commands.cluster_info
import bakar.commands.diff
import bakar.commands.diffsigs
import bakar.commands.doctor
import bakar.commands.drift
import bakar.commands.dump
import bakar.commands.for_all
import bakar.commands.gen_kas
import bakar.commands.getvar
import bakar.commands.graph
import bakar.commands.hashserv
import bakar.commands.init
import bakar.commands.inspect
import bakar.commands.layers
import bakar.commands.lock
import bakar.commands.log
import bakar.commands.mirror
import bakar.commands.monitor
import bakar.commands.override
import bakar.commands.prefetch
import bakar.commands.presets
import bakar.commands.prserv
import bakar.commands.report
import bakar.commands.sched_triage
import bakar.commands.settings
import bakar.commands.setup
import bakar.commands.shell
import bakar.commands.show
import bakar.commands.stop
import bakar.commands.stress_parse
import bakar.commands.sync
import bakar.commands.triage  # noqa: F401
from bakar.commands import app
from bakar.commands._app import console
from bakar.steps.kas_build import BitbakeBinMissingError, BuildtoolsMissingError

__all__ = ["app", "main"]


def main() -> int:
    """Run the bakar CLI with plain (non-Rich-panel) error output."""
    try:
        # standalone_mode=False prevents Click from calling sys.exit AND prevents
        # Typer's rich_utils from rendering UsageError/BadParameter inside a Panel.
        return app(standalone_mode=False) or 0
    except _click_exc.UsageError as exc:
        # Captures NoSuchOption, MissingParameter, BadParameter, and bare UsageError.
        ctx = exc.ctx
        if ctx is not None:
            console.print(ctx.get_usage())
            console.print(f"Try '{ctx.command_path} --help' for help.")
        console.print(f"Error: {exc.format_message()}")
        return exc.exit_code if exc.exit_code is not None else 2
    except _click_exc.ClickException as exc:
        # Non-usage Click errors (e.g. FileError, BadOptionUsage, custom ClickException).
        console.print(f"Error: {exc.format_message()}")
        return exc.exit_code if exc.exit_code is not None else 1
    except _click_exc.Abort:
        # SIGINT during a prompt; Click convention is exit 1 with no traceback.
        console.print("Aborted.")
        return 1
    except (_click_exc.Exit, typer.Exit) as exc:
        # typer.Exit (used everywhere in our commands) -> the carried exit code.
        return int(exc.exit_code) if getattr(exc, "exit_code", 0) is not None else 0
    except (BuildtoolsMissingError, BitbakeBinMissingError) as exc:
        # A host build/inspection prerequisite (the pinned buildtools-extended
        # toolchain or its bitbake bin) is missing. The exception message names the
        # missing toolchain and the fix, so surface it cleanly - a read-only
        # `bakar getvar`/`dump` on a stock host must not dump a raw traceback.
        console.print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

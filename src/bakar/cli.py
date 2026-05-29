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

import bakar.commands.build  # noqa: F401
import bakar.commands.clean  # noqa: F401
import bakar.commands.clean_sstate  # noqa: F401
import bakar.commands.diff  # noqa: F401
import bakar.commands.doctor  # noqa: F401
import bakar.commands.dump  # noqa: F401
import bakar.commands.for_all  # noqa: F401
import bakar.commands.gen_kas  # noqa: F401
import bakar.commands.hashserv  # noqa: F401
import bakar.commands.layers  # noqa: F401
import bakar.commands.lock  # noqa: F401
import bakar.commands.log  # noqa: F401
import bakar.commands.override  # noqa: F401
import bakar.commands.prefetch  # noqa: F401
import bakar.commands.report  # noqa: F401
import bakar.commands.settings  # noqa: F401
import bakar.commands.shell  # noqa: F401
import bakar.commands.stress_parse  # noqa: F401
import bakar.commands.sync  # noqa: F401
import bakar.commands.triage  # noqa: F401
from bakar.commands import app  # noqa: F401 - re-exported for pyproject.toml entry point
from bakar.commands._app import console

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


if __name__ == "__main__":
    sys.exit(main())

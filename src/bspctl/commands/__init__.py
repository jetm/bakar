"""bspctl command sub-package.

Importing this package registers all subcommands on ``app``. Use
``from bspctl.commands import app`` to get the fully-wired Typer app.
"""

from bspctl.commands._app import app, console

__all__ = ["app", "console"]

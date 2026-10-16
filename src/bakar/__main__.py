"""Entry point for `python -m bakar`."""

import sys

from bakar.cli import main

if __name__ == "__main__":
    # main() carries the plain-error handling the `bakar` script gets (a
    # RepoSyncError prints a message instead of a traceback); app() alone skips it.
    sys.exit(main())

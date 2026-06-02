#!/usr/bin/env python3
"""Drive the live build UI with a scripted, accelerated build for a recording.

Replays a realistic sequence of knotty fallback lines through ``BuildUIState``
and a Rich ``Live`` display so VHS (``scripts/demo_build_ui.tape``) can record
``docs/build-ui.gif``. This is a recording helper only -- not used at runtime.

    vhs scripts/demo_build_ui.tape   # or: uv run python scripts/demo_build_ui.py
"""

from __future__ import annotations

import logging
import sys
import time

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler

from bakar.steps.build_ui import BuildUIState


def _start(pf: str, task: str) -> str:
    return f"NOTE: recipe {pf}: task {task}: Started"


def _done(pf: str, task: str) -> str:
    return f"NOTE: recipe {pf}: task {task}: Succeeded"


def _count(n: int) -> str:
    return f"NOTE: Running setscene task {n} of 5944 (/x.bb:do_x_setscene)"


def main() -> None:
    # Wipe the invoking command line so the recording shows only the UI.
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()

    # A RichHandler logger on the same console as Live, matching RunLogger's
    # setup, so the parse-complete line renders with a real INFO tag.
    console = Console()
    logger = logging.getLogger("demo-build")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(RichHandler(console=console, show_time=False, show_path=False, markup=True))

    # Backdate the bakar start so the global timer reads ~1h02m, consistent with
    # the stuck glibc compile below (a real build that has been running that long).
    ui = BuildUIState(start_monotonic=time.monotonic() - 3750)
    with Live(get_renderable=ui.make_renderable, console=console, refresh_per_second=12, screen=False):
        # --- parse phase: the percentage bar ramps to 100% ---
        for pct in (0, 14, 30, 47, 63, 80, 93, 100):
            ui.process_line(f"Parsing recipes: {pct}% || ETA:  0:00:18")
            time.sleep(0.3)
        time.sleep(0.5)

        # --- build phase: enter setscene, recipes start churning ---
        # The first counter line queues the one-time parse-complete log.
        ui.process_line(_count(1))
        info = ui.take_pending_log()
        if info:
            logger.info(info)
        # glibc is the stuck recipe: backdate its start so it renders red
        # (far past the running-set median) for the whole recording.
        ui.process_line(_start("glibc-2.39-r0", "do_compile"))
        ui._running["glibc-2.39-r0:do_compile"].start -= 3725
        time.sleep(0.5)

        ui.process_line(_start("webkitgtk-2.44.1-r0", "do_compile"))
        time.sleep(0.4)
        ui.process_line(_count(140))
        ui.process_line(_start("linux-firmware-20240101", "do_fetch"))
        time.sleep(0.4)
        ui.process_line(_start("python3-3.12.0-r0", "do_configure"))
        time.sleep(0.4)
        ui.process_line(_start("mesa-24.0.7-r0", "do_package_write_rpm"))
        time.sleep(0.6)

        ui.process_line(_count(430))
        ui.process_line(_done("linux-firmware-20240101", "do_fetch"))
        ui.process_line(_start("gcc-runtime-13.2.0-r0", "do_compile"))
        time.sleep(0.6)
        ui.process_line(_done("python3-3.12.0-r0", "do_configure"))
        ui.process_line(_start("ncurses-6.4-r0", "do_configure"))
        ui.process_line(_count(910))
        time.sleep(0.6)

        ui.process_line(_done("mesa-24.0.7-r0", "do_package_write_rpm"))
        ui.process_line(_start("busybox-1.36.1-r0", "do_compile"))
        ui.process_line(_count(1480))
        time.sleep(0.6)
        ui.process_line(_done("gcc-runtime-13.2.0-r0", "do_compile"))
        ui.process_line(_start("u-boot-2024.01-r0", "do_compile"))
        ui.process_line(_done("ncurses-6.4-r0", "do_configure"))
        ui.process_line(_start("openssl-3.2.0-r0", "do_compile"))
        ui.process_line(_count(2120))
        time.sleep(0.7)

        ui.process_line(_done("busybox-1.36.1-r0", "do_compile"))
        ui.process_line(_start("systemd-255.4-r0", "do_compile"))
        ui.process_line(_count(2336))

        # Hold on the final frame so the stuck (red) glibc row is legible. The
        # VHS tape stops recording during this hold, so the GIF ends on the UI
        # rather than the shell prompt that appears after the program exits.
        time.sleep(5.0)


if __name__ == "__main__":
    main()

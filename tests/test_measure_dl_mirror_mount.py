"""scripts/measure_dl_mirror_mount.sh: it stops promptly on a signal and still reports its rows."""

from __future__ import annotations

import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_dl_mirror_mount.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_terminate_during_a_timeout_wrapped_row_exits_130_and_prints_rows(tmp_path: Path) -> None:
    # The live-fetch row runs the smoke under GNU timeout, which sits in its own process
    # group: without the wait-based handler the script would sit out the whole timeout.
    mount = tmp_path / "mnt"
    (mount / "downloads").mkdir(parents=True)
    (mount / "downloads" / "a.bin").write_bytes(b"data")
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(60)\n")
    out = tmp_path / "out.txt"
    with out.open("w") as fh:
        proc = subprocess.Popen(
            ["bash", str(_SCRIPT), "--mount", str(mount), "--smoke", str(sleeper)],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # Wait until the rows before the live fetch have printed, so the signal lands
        # while the sleeper is the child being waited on.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "read-throughput" not in out.read_text():
            time.sleep(0.1)
        time.sleep(0.5)
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        elapsed = time.monotonic() - start
    assert rc == 130
    assert elapsed < 5
    assert "rows:" in out.read_text()

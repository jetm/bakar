"""A meta-avocado BYO build records the machine its kas YAML resolves to.

Drives the real chain: ``resolve()`` reads the machine through
``machine_from_yaml``, and ``_record_build_config`` writes it to the run's
``events.jsonl``, which ``last_build_config`` reads back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bakar.bsp_detect import machine_from_yaml
from bakar.config import ResolveRequest, resolve
from bakar.observability import RunLogger, last_build_config
from bakar.steps.kas_build import _record_build_config

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

MACHINE = "avocado-jetson-agx-orin"


def test_byo_meta_avocado_build_records_yaml_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BAKAR_BSP_FAMILY", "BAKAR_MACHINE", "BAKAR_DISTRO", "BAKAR_IMAGE"):
        monkeypatch.delenv(var, raising=False)
    kas_dir = tmp_path / "sources" / "meta-avocado" / "kas" / "machine"
    kas_dir.mkdir(parents=True)
    kas_yaml = kas_dir / f"{MACHINE}.yml"
    kas_yaml.write_text(f"header:\n  version: 14\nmachine: {MACHINE}\ntarget: avocado-image\n")
    assert machine_from_yaml(kas_yaml) == MACHINE

    cfg = resolve(ResolveRequest(workspace=tmp_path, bsp_family="generic", kas_yaml=kas_yaml))

    assert cfg.machine == MACHINE
    with RunLogger(tmp_path / "runs") as log:
        _record_build_config(cfg, log)
        run_dir = log.run_dir

    recorded = last_build_config(run_dir)
    assert recorded is not None
    assert recorded["machine"] == MACHINE

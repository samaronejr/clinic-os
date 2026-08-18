"""Execute final-wave children through the durable process barrier."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing.isolation_barrier_process import start_barrier_process
from ops.testing.isolation_common import utc_now

if TYPE_CHECKING:
    from ops.testing.isolation_final_wave_controller import (
        _FinalWaveSession as FinalWaveSession,
    )


def _run_final_child(
    session: FinalWaveSession,
    payload: tuple[str, ...],
    environment: dict[str, str],
) -> int:
    lease = Path(str(session.record["controller_lease_path"]))
    stage = str(session.record.get("stage") or session.record["form"])
    stdout = lease.with_name(f"{stage}.stdout")
    stderr = lease.with_name(f"{stage}.stderr")
    child = start_barrier_process(payload, environment, stdout, stderr)
    try:
        session.record_child(child.identity, utc_now())
        child.release()
        session.release_child(utc_now())
        wait = child.wait(2160)
        session.record_wait(wait, utc_now())
    finally:
        child.close()
        stdout.unlink(missing_ok=True)
        stderr.unlink(missing_ok=True)
    if wait.exit_code is not None:
        return wait.exit_code
    return 128 + (wait.signal or 0)


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _start_ticks(pid: int) -> int:
    return int((Path("/proc") / str(pid) / "stat").read_text().split()[21])


def _process_is_live(pid: int, ticks: int) -> bool:
    try:
        return _start_ticks(pid) == ticks
    except FileNotFoundError:
        return False

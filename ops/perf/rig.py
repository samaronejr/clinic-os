"""Owner-approved server budgets, separate from external flood producers."""

from __future__ import annotations

import multiprocessing
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self
from uuid import uuid4

from ops.perf.runtime import PerfEnvironmentError

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from types import TracebackType

Role = Literal["application", "clinical", "worker", "producers"]
GIB = 1024**3
CGROUP_ROOT = Path("/sys/fs/cgroup")
PREFIX_ENV = "CLINIC_PERF_RESOURCE_RIG"
BUDGETS: dict[Role, tuple[int, int]] = {
    "application": (2, 4 * GIB),
    "clinical": (1, 3 * GIB),
    "worker": (1, GIB),
    "producers": (2, GIB),
}


def _hold_launcher(prefix: str, channel: Connection) -> None:
    """Keep even a singleton launcher scope alive until placement is restored."""
    with channel:
        channel.send_bytes(b"ready")
        try:
            channel.recv_bytes()
        except EOFError:
            _command(
                "/usr/bin/systemctl",
                "stop",
                _slice(prefix, "application"),
                _slice(prefix, "producers"),
            )


def _command(
    program: Literal["/usr/bin/busctl", "/usr/bin/systemctl"], *arguments: str
) -> str:
    environment = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    try:
        result = subprocess.run(  # noqa: S603 - only fixed systemd executables below
            [program, "--user", *arguments],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        message = (
            f"performance rig {program} failed ({error.returncode}): "
            f"{str(error.stderr).strip()}"
        )
        raise PerfEnvironmentError(message) from error
    except OSError as error:
        message = f"performance rig cannot execute {program}: {error.strerror}"
        raise PerfEnvironmentError(message) from error
    return result.stdout.strip()


def _bus(method: str, signature: str, *arguments: str) -> str:
    return _command(
        "/usr/bin/busctl",
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        method,
        signature,
        *arguments,
    )


def _transient(name: str, properties: list[list[str]]) -> None:
    _bus(
        "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        name,
        "fail",
        str(len(properties)),
        *(value for row in properties for value in row),
        "0",
    )


def _prefix() -> str:
    prefix = os.environ.get(PREFIX_ENV, "")
    if re.fullmatch(r"cpiperf[0-9a-f]{32}", prefix) is None:
        message = "performance workers require the canonical resource rig"
        raise PerfEnvironmentError(message)
    return prefix


def _slice(prefix: str, role: Role) -> str:
    if role == "application":
        return f"{prefix}.slice"
    if role == "producers":
        return f"{prefix}producers.slice"
    return f"{prefix}-{role}.slice"


def _path(prefix: str, role: Role) -> Path:
    value = _command(
        "/usr/bin/systemctl",
        "show",
        _slice(prefix, role),
        "-p",
        "ControlGroup",
        "--value",
    )
    if not value.startswith("/") or _slice(prefix, role) not in value.split("/"):
        message = "performance rig control group does not match its declared role"
        raise PerfEnvironmentError(message)
    return CGROUP_ROOT / value.lstrip("/")


def _snapshot(prefix: str, role: Role) -> dict[str, int | str]:
    path = _path(prefix, role)
    quota, period = (path / "cpu.max").read_text().split()
    cores, memory = BUDGETS[role]
    if (
        quota == "max"
        or int(quota) != cores * int(period)
        or (path / "memory.max").read_text().strip() != str(memory)
        or (path / "memory.swap.max").read_text().strip() != "0"
    ):
        message = "performance rig CPU, memory or swap budget does not match"
        raise PerfEnvironmentError(message)
    cpu = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
    return {
        "cpu_cores": cores,
        "memory_bytes": memory,
        "swap_bytes": 0,
        "cpu_usage_usec": int(cpu["usage_usec"]),
        "cpu_throttled_usec": int(cpu["throttled_usec"]),
        "cpu_pressure": (path / "cpu.pressure").read_text().strip(),
        "memory_current_bytes": int((path / "memory.current").read_text()),
        "memory_events": (path / "memory.events").read_text().strip(),
        "control_group": str(path.relative_to(CGROUP_ROOT)),
    }


def resource_snapshot() -> dict[str, dict[str, int | str]]:
    """Read actual kernel accounting, including exited children, for each role."""
    prefix = _prefix()
    return {role: _snapshot(prefix, role) for role in BUDGETS}


def attach(role: Literal["clinical", "worker", "producers"]) -> None:
    """Move this process before executing business code or producer bookkeeping."""
    prefix = _prefix()
    name = f"{prefix}-{role}-{os.getpid()}.scope"
    _transient(
        name,
        [
            ["Description", "s", f"Clinic performance {role}"],
            ["Slice", "s", _slice(prefix, role)],
            ["PIDs", "au", "1", str(os.getpid())],
            ["CollectMode", "s", "inactive-or-failed"],
        ],
    )
    _command("/usr/bin/systemctl", "start", name)
    actual = Path("/proc/self/cgroup").read_text().strip().split(":")[-1]
    if _slice(prefix, role) not in actual.split("/"):
        message = "performance process did not enter its declared resource role"
        raise PerfEnvironmentError(message)
    _snapshot(prefix, role)


class ResourceRig:
    """Enforce 2 CPU/4 GiB application total; producers are a separate sibling."""

    def __init__(self) -> None:
        """Allocate unique, task-owned systemd names without creating resources."""
        self.prefix = f"cpiperf{uuid4().hex}"
        self.original_unit = Path(
            Path("/proc/self/cgroup").read_text().strip().split(":")[-1]
        ).name
        self.created: list[str] = []
        self.attached = False
        self.anchor: BaseProcess | None = None
        self.anchor_channel: Connection | None = None
        self.anchor_ready = False

    def _start_anchor(self) -> None:
        """Keep the original invocation alive before any process relocation."""
        spawn = multiprocessing.get_context("spawn")
        self.anchor_channel, child = spawn.Pipe()
        self.anchor = spawn.Process(target=_hold_launcher, args=(self.prefix, child))
        try:
            self.anchor.start()
        finally:
            child.close()
        if (
            not self.anchor_channel.poll(30)
            or self.anchor_channel.recv_bytes() != b"ready"
        ):
            message = "performance rig launcher anchor did not start"
            raise PerfEnvironmentError(message)
        self.anchor_ready = True

    def __enter__(self) -> Self:
        """Create and verify all budgets, then put the API process in its share."""
        if PREFIX_ENV in os.environ:
            message = "performance resource rigs cannot be nested"
            raise PerfEnvironmentError(message)
        state = _command(
            "/usr/bin/systemctl",
            "show",
            self.original_unit,
            "-p",
            "Delegate",
            "-p",
            "ActiveState",
        )
        properties = dict(line.split("=", 1) for line in state.splitlines())
        if properties.get("Delegate") != "yes" or properties.get("ActiveState") not in {
            "active",
            "activating",
        }:
            message = (
                "performance rig requires an active delegated launcher "
                "(Delegate=cpu memory pids) before relocating processes"
            )
            raise PerfEnvironmentError(message)
        try:
            self._start_anchor()
            for role, (cores, memory) in BUDGETS.items():
                name = _slice(self.prefix, role)
                _transient(
                    name,
                    [
                        ["Description", "s", f"Clinic performance {role} budget"],
                        ["BindsTo", "as", "1", self.original_unit],
                        ["CPUQuotaPerSecUSec", "t", str(cores * 1_000_000)],
                        ["MemoryMax", "t", str(memory)],
                        ["MemorySwapMax", "t", "0"],
                    ],
                )
                self.created.append(name)
                _command("/usr/bin/systemctl", "start", name)
            os.environ[PREFIX_ENV] = self.prefix
            self.attached = True
            attach("clinical")
            resource_snapshot()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Restore only our process and release only this invocation's groups."""
        try:
            if self.attached:
                _bus(
                    "AttachProcessesToUnit",
                    "ssau",
                    self.original_unit,
                    "",
                    "1",
                    str(os.getpid()),
                )
                self.attached = False
            for name in reversed(self.created):
                _command("/usr/bin/systemctl", "stop", name)
            self.created.clear()
            if self.anchor_ready and self.anchor_channel is not None:
                self.anchor_channel.send_bytes(b"restored")
        except PerfEnvironmentError as failure:
            sys.stderr.write(f"perf rig teardown: {failure}\n")
            sys.stderr.flush()
            raise
        finally:
            os.environ.pop(PREFIX_ENV, None)
            if self.anchor_channel is not None:
                self.anchor_channel.close()
            if self.anchor is not None and self.anchor.pid is not None:
                self.anchor.join(30)
                if self.anchor.is_alive():
                    self.anchor.terminate()
                    self.anchor.join()
                if self.anchor.exitcode != 0:
                    message = "performance rig launcher anchor did not stop cleanly"
                    raise PerfEnvironmentError(message)

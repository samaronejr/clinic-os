from __future__ import annotations

import contextlib
import io
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from isolation.execution_host_preflight_probe import Checkpoint, EventProbe

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PUBLISHER: Final = PROJECT_ROOT / "ops/testing/execution_host_preflight.py"
BOOT_ID: Final = "11111111-1111-4111-8111-111111111111"


@dataclass(slots=True)
class PreflightHarness:
    workspace: Path
    authority_root: Path
    cgroup_mount: Path
    cgroup_parent: Path
    artifact: Path
    pending: Path
    archive_state: Path
    archive_sentinel: Path
    stable_lock: Path

    @classmethod
    def create(cls, root: Path) -> PreflightHarness:
        workspace = root / "authority"
        authority_root = workspace / ".omo"
        (authority_root / "plans").mkdir(parents=True)
        (authority_root / "evidence").mkdir()
        plan = authority_root / "plans/clinic-os-phase-1a-staff-scheduling.md"
        plan.write_bytes(b"# approved\n")
        cgroup_mount = root / "cgroup"
        cgroup_parent = cgroup_mount / "delegated"
        cgroup_parent.mkdir(parents=True)
        for name in ("cgroup.events", "cgroup.kill", "cgroup.procs"):
            (cgroup_parent / name).write_bytes(b"0\n")
        artifact = authority_root / f"clinic-os-phase1a-execution-host-{BOOT_ID}.json"
        return cls(
            workspace,
            authority_root,
            cgroup_mount,
            cgroup_parent,
            artifact,
            artifact.with_name(f".{artifact.name}.pending"),
            authority_root / "evidence/isolation-archive-rollover-phase1a.json",
            authority_root / "evidence/isolation-archive-rollover-phase1a.sentinel",
            authority_root / "evidence/isolation-ledger-phase1a.lock",
        )

    def run(
        self,
        checkpoint: Checkpoint | None = None,
        *,
        stale_journal: Path | None = None,
        stable_lock_descriptor: int | None = None,
    ) -> str:
        probe = EventProbe(self, checkpoint)
        output = io.StringIO()
        original_read_text = Path.read_text
        original_resolve = Path.resolve

        def read_text(
            path: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if path == Path("/proc/sys/kernel/random/boot_id"):
                return f"{BOOT_ID}\n"
            if path == Path("/proc/self/cgroup"):
                return "0::/delegated\n"
            if path == Path("/proc/self/mountinfo"):
                return f"1 0 0:1 / {self.cgroup_mount} rw - cgroup2 cgroup rw\n"
            return original_read_text(path, encoding=encoding, errors=errors)

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == Path("/sys/fs/cgroup"):
                return self.cgroup_mount
            return original_resolve(path, strict=strict)

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(Path, "read_text", read_text)
            monkeypatch.setattr(Path, "resolve", resolve)
            monkeypatch.setattr(
                sys,
                "argv",
                self._arguments(stale_journal, stable_lock_descriptor),
            )
            probe.install(monkeypatch)
            try:
                with contextlib.redirect_stdout(output):
                    runpy.run_path(str(PUBLISHER), run_name="__main__")
            finally:
                probe.close_leaked_descriptors()
        return output.getvalue().strip()

    def create_stable_lock(self) -> None:
        self.stable_lock.write_bytes(b"")
        self.stable_lock.chmod(0o600)

    def _arguments(
        self,
        stale_journal: Path | None,
        stable_lock_descriptor: int | None,
    ) -> list[str]:
        arguments = [
            str(PUBLISHER),
            "publish",
            "--authority-root",
            str(self.authority_root),
        ]
        if stale_journal is None and stable_lock_descriptor is None:
            return arguments
        if stale_journal is None or stable_lock_descriptor is None:
            message = "stale journal and inherited lock descriptor must be paired"
            raise ValueError(message)
        return [
            *arguments,
            "--stale-boot-resume",
            str(stale_journal),
            "--stable-lock-fd",
            str(stable_lock_descriptor),
        ]

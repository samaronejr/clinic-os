"""Output-only observer of the one lifecycle race and its actual cgroup budget."""
import json
import os
import resource
import sys
import time
from pathlib import Path

import pytest

NODE = "tests/infra/test_lifecycle_lock_races.py::test_cross_clinic_equal_canonical_email_waits_on_one_global_gate"
OBS = {}

def native_interpreter():
    executable=Path(sys.executable)
    system=Path(sys._base_executable)
    assert executable.relative_to(Path.cwd()).as_posix()==".venv/bin/python"
    assert system==Path("/usr/bin/python3.12") and executable.samefile(system)
    assert sys.implementation.name=="cpython" and sys.version_info[:3]==(3,12,3)
    assert os.pidfd_open.__module__=="posix"
    fd=os.pidfd_open(os.getpid())
    try:
        target=os.readlink(f"/proc/self/fd/{fd}")
        assert target=="anon_inode:[pidfd]"
    finally:
        os.close(fd)
    return {"executable_relative_to_checkout":".venv/bin/python","native_executable":str(system),
            "implementation":"CPython","version":sys.version.split()[0],"pidfd_provider":"posix",
            "kernel_fd_target":target,"descriptor_closed":True}

def pytest_sessionstart(session):
    path=os.environ.get("R4E_SC5_INTERPRETER")
    if path and not session.config.option.collectonly:
        witness=native_interpreter()
        witness.update(run_id=os.environ["INVOCATION_ID"],sha=os.environ["R4E_CERT_SHA"],phase="pytest_sessionstart")
        with Path(path).open("x",encoding="ascii") as f:
            json.dump(witness,f,sort_keys=True)

def snapshot():
    cgroup, = [l[3:] for l in Path("/proc/self/cgroup").read_text().splitlines() if l.startswith("0::")]
    root = Path("/sys/fs/cgroup")
    here = root / cgroup.lstrip("/")
    quotas = []
    for p in (here, *here.parents):
        if p == root:
            break
        quotas.append((p/"cpu.max").read_text().strip())
    stats = {k:int(v) for k,v in (l.split() for l in (here/"cpu.stat").read_text().splitlines())}
    pressures = {name:int((here/(name+".pressure")).read_text().splitlines()[0].split("total=")[1]) for name in ("cpu","io","memory")}
    return {"ns":time.monotonic_ns(),"cpu_max":quotas,"cpu_us":stats["usage_usec"],
            "throttled_us":stats.get("throttled_usec",0),"throttled_periods":stats.get("nr_throttled",0),
            "pressure_us":pressures,"major_faults":resource.getrusage(resource.RUSAGE_SELF).ru_majflt}

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.nodeid != NODE:
        yield
        return
    OBS["before"] = snapshot()
    try:
        yield
    finally:
        OBS["after"] = snapshot()

def pytest_runtest_logreport(report):
    if report.nodeid == NODE and report.when == "call":
        # Never serialize freeform output or local variables.
        crash = getattr(report.longrepr, "reprcrash", None)
        msg = getattr(crash, "message", "")
        OBS.update(run_id=os.environ["INVOCATION_ID"],node=NODE,phase=report.when,outcome=report.outcome,
                   duration_s=report.duration,at_commit_deadline="management token commits did not finish" in msg)
        with Path(os.environ["R4E_RACE_TIMING"]).open("x",encoding="ascii") as f:
            json.dump(OBS,f,sort_keys=True)

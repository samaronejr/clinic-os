"""Transfer the actual test's safe handoff property, never synthesize timing samples."""
import json
import os
from pathlib import Path

import pytest

NODE="tests/teleconsult/test_mode_migration.py::test_same_lease_posture_after_committed_migration"

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    report=yield
    if item.nodeid==NODE and report.when=="call":
        records=[value for key,value in item.user_properties if key=="posture-handoff"]
        if records:
            payload={"node_id":NODE,"outcome":report.outcome,"phase":report.when,"sha":os.environ["R4E_SHA"],
                     "run_id":os.environ["INVOCATION_ID"],"trace":json.loads(records[-1])}
            with Path(os.environ["R4E_TRACE"]).open("x") as f:json.dump(payload,f,sort_keys=True,separators=(",",":"))
    return report

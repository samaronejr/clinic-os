"""Run r4g's *literal*, unmodified socket-receipt omission against r4h.

It must refuse normal admission. A constructor interruption must still get
reaped by the independent parent-held birth handle with the same omission.
"""
import json,os,sys,time
from pathlib import Path
from unittest.mock import patch
import pytest
from sc5_report import public_id
sys.path.insert(0,str(Path.cwd()/'tests'))
from infra import posture_child_lifetime as custody
from infra import posture_handoff as handoff

PARTIAL='tests/infra/test_posture_signal_custody.py::test_lost_number_cannot_authorize_error_cleanup[partial-spawn]'
ADMIT='tests/teleconsult/test_mode_migration.py::test_same_lease_posture_after_committed_migration'
cases={}
def lose(_sock):raise RuntimeError('synthetic receipt discarded')
@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    if item.nodeid not in (PARTIAL,ADMIT):raise AssertionError('unexpected compatibility test')
    with patch.object(custody,'_receive',lose):
        yield
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    rep=yield
    if rep.when=='call':
        if item.nodeid==ADMIT:
            actual=bool(call.excinfo and call.excinfo.type is RuntimeError and str(call.excinfo.value)=='synthetic receipt discarded' and call.excinfo.traceback[-1].name=='lose')
        else:
            rows=[v for k,v in item.user_properties if k=='posture-custody'];actual=bool(rep.passed and len(rows)==1 and rows[0]['partial_child_reaped'] and not rows[0]['foreign_signals'])
        cases[public_id(item.nodeid)]={'outcome':rep.outcome,'intended':actual}
    return rep
def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ['R4H_COMPAT']).open('x') as f:json.dump(dict(sha=os.environ['R4E_SHA'],run_id=os.environ['INVOCATION_ID'],exit=int(exitstatus),cases=cases),f,sort_keys=True)

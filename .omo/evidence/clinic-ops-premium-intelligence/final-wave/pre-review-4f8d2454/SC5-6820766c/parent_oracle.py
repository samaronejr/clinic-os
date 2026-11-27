"""Keep only verdicts from the original call assertions, never exception text."""
import json,os
from pathlib import Path
import pytest
from sc5_report import public_id

cases={}
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    rep=yield
    if rep.when=='call':
        group=item.nodeid.endswith('test_recycled_group_never_receives_an_owned_cleanup_signal')
        schema=item.nodeid.endswith('[schema]')
        if schema:result=rep.passed and call.excinfo is None
        else:
            expect='== []' if group else 'assert not True'
            result=rep.failed and call.excinfo is not None and call.excinfo.type is AssertionError and expect in str(call.excinfo.value)
        cases[public_id(item.nodeid)]=dict(outcome=rep.outcome,intended=bool(result),prior_schema_guard=schema,signal_model=group)
    return rep
def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ['R4F_PARENT_CASES']).open('x') as f:json.dump(dict(parent='a3013b5b2fe360e1502f459f325ceb6be0de4469',successor=os.environ['R4E_SHA'],run_id=os.environ['INVOCATION_ID'],exit=int(exitstatus),cases=cases),f,sort_keys=True)

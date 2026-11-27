"""Persist only the test's closed-set observations, not argv, exception text or identities."""
import ast,json,os
from pathlib import Path
import pytest
from sc5_report import public_id
BASE='6820766c6d02ae9baaca01e09facd0a14167ce0c'
NODE='tests/infra/test_posture_signal_custody.py::test_lost_number_cannot_authorize_error_cleanup'
CASES={NODE+'['+n+']':(disp,partial) for n,disp,partial in [
    ('inherited-ignore','ignored',False),('inherited-default','default',False),('partial-spawn','ignored',True)]}
records={}
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    rep=yield
    if rep.when!='call' or item.nodeid not in CASES:return rep
    (disp,partial)=CASES[item.nodeid]; red=os.environ['R4G_ROLE']=='parent'
    row,=[v for k,v in rep.user_properties if k=='posture-custody']
    assert row==dict(inherited=disp,default_at_spawn=(not red or disp=='default'),
        restored=True,foreign_signals=int(red and not partial),pidfd_signals=int(not red),partial_spawn=partial,
        partial_child_reaped=bool(partial and not red))
    intended=rep.passed and call.excinfo is None
    if red:
        intended=False
        if call.excinfo and call.excinfo.type is AssertionError:
            tb=call.excinfo.traceback[-1];line=tb.lineno+1
            expr=next((n for n in ast.walk(ast.parse(Path(item.path).read_text())) if isinstance(n,ast.Assert) and n.lineno<=line<=n.end_lineno),None)
            intended=bool(str(tb.path)==str(item.path) and expr and ast.unparse(expr.test)==('kernel.reaped' if partial else 'kernel.foreign == []'))
    records[public_id(item.nodeid)]=dict(outcome=rep.outcome,intended=intended,observations=row)
    return rep
def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ['R4G_CUSTODY']).open('x') as f:json.dump(dict(parent=BASE,successor=os.environ['R4E_SHA'],target=os.environ['R4G_ROLE'],run_id=os.environ['INVOCATION_ID'],exit=int(exitstatus),cases=records),f,sort_keys=True)

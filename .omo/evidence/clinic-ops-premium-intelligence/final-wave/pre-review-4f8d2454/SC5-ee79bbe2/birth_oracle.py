"""Bind six discriminating round-seven assertions to native pytest reports."""
import ast,json,os
from pathlib import Path
import pytest
from sc5_report import public_id
BASE='26a2a86b4873b2bcfea74ac42a9e7700a0661d14'
PREFIX='tests/infra/test_posture_birth_custody.py::'
MISSING=PREFIX+'test_interrupted_outer_without_usable_receipt_is_already_reaped'
DRIVER=PREFIX+'test_orphan_driver_timeout_uses_handle_runner'
STATIC='tests/infra/test_kek_signal_audit.py::test_entire_kek_lane_has_no_numeric_signal_fallback'
CASES={**{MISSING+'['+v+']':'already_reaped' for v in ('eof','malformed','unreadable','silent-live')},DRIVER:'numeric == []',STATIC:'audit.audit(sources) == []'}
records={}
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    report=yield
    if report.when!='call' or item.nodeid not in CASES:return report
    red=os.environ['R4H_ROLE']=='parent'
    props=[v for k,v in report.user_properties if k=='birth-custody']
    if item.nodeid.startswith(MISSING+'['):
        row,=props;variant=item.nodeid.split('[')[-1][:-1]
        assert row==dict(variant=variant,already_reaped=not red,foreign_signals=0,owned_pidfd_signals=int(not red))
    else:
        assert not props
        row={}
    intended=report.passed and call.excinfo is None
    if red:
        intended=False
        if call.excinfo and call.excinfo.type is AssertionError:
            tb=call.excinfo.traceback[-1];line=tb.lineno+1
            expr=next((n for n in ast.walk(ast.parse(Path(item.path).read_text())) if isinstance(n,ast.Assert) and n.lineno<=line<=n.end_lineno),None)
            intended=bool(str(tb.path)==str(item.path) and expr and ast.unparse(expr.test)==CASES[item.nodeid])
    records[public_id(item.nodeid)]=dict(outcome=report.outcome,intended=intended,observations=row)
    return report
def pytest_sessionfinish(session,exitstatus):
    with Path(os.environ['R4H_BIRTH']).open('x') as f:
        json.dump(dict(parent=BASE,successor=os.environ['R4E_SHA'],target=os.environ['R4H_ROLE'],run_id=os.environ['INVOCATION_ID'],exit=int(exitstatus),cases=records),f,sort_keys=True)

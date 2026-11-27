"""Independent validation of the actual joint warm/fresh/injected-timeout witness."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NODE="tests/teleconsult/test_mode_migration.py::test_same_lease_posture_after_committed_migration"
PHASES=["peer-connect","peer-query","role-query","routine-query","peer-finished"]
BEGIN=["make-launch","uv-launch","verifier-start","primary-connect","primary-query","primary-closed"]

def states(rows):
    for row in rows:
        assert set(row)=={"role","state","wait_type","wait","count","oldest_tx_ms","blocked"}
        assert row["role"] in {"app","inspector","other"}
        assert row["state"] in {"active","idle","idle in transaction","idle in transaction (aborted)","fastpath function call","disabled",None}
        assert all(x is None or re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*",x) for x in (row["wait"],row["wait_type"]))
        assert all(type(row[k])==int and row[k]>=0 for k in ("count","oldest_tx_ms","blocked"))
        assert 0<row["count"] and row["blocked"]<=row["count"]

def processes(rows):
    for row in rows:
        assert set(row)=={"process","state","wchan","major_faults","cpu_ticks"}
        assert row["process"] in {"make","sh","bash","uv","python","python3","python3.12","other"}
        assert re.fullmatch(r"[A-Za-z]",row["state"]) and re.fullmatch(r"[\w.+-]*",row["wchan"])
        assert all(type(row[k])==int and row[k]>=0 for k in ("cpu_ticks","major_faults"))

def check(root,sha):
    root=Path(root);p=json.loads((root/"handoff.json").read_bytes());d=p['trace']
    assert set(p)=={'sha','run_id','node_id','phase','outcome','trace'} and p["sha"]==sha and p["node_id"]==NODE
    assert p["phase"]=="call" and p["outcome"]=="passed"
    suite,=ET.parse(root/"focused.xml").iter("testsuite")
    assert suite.get('name')=='r4f-positive:'+sha+':'+p['run_id']
    case,=[c for c in suite if c.get('name')==NODE];assert not len(case)
    assert set(d)=={'checkpoints','idle_ns','same_lease','distinct_fresh_control','fresh','warm','injected_launcher_wait'}
    assert d['same_lease'] is d['distinct_fresh_control'] is True and d['idle_ns']>0
    cps=d['checkpoints'];assert [c['stage'] for c in cps]==['before-prior-consumer','after-prior-teardown','before-second-consumer']
    assert all(set(c)=={'stage','ns','backends'} and c['backends']==[] for c in cps)
    assert cps[0]['ns']<cps[1]['ns']<cps[2]['ns'] and cps[2]['ns']-cps[1]['ns']>=d['idle_ns']
    out={'run_id':p['run_id'],'idle_ns':d['idle_ns'],'same_lease':True,'fresh_control_distinct':True}
    for name in ('warm','fresh','injected_launcher_wait'):
        q=d[name];fault=name=='injected_launcher_wait'
        assert set(q)=={'before','after','elapsed_ns','end_ns','exit','group_reaped','last_sample_before_deadline','limit_seconds','processes_at_deadline','samples','stages','start_ns','timed_out'}
        assert q['limit_seconds']==15 and q['timed_out'] is fault and q['group_reaped'] is True
        assert q['before']==q['after']==[] and q['elapsed_ns']==q['end_ns']-q['start_ns']>0
        ms=q['stages'];assert all(set(m)=={'ns','stage','ordinal'} and type(m['ns'])==type(m['ordinal'])==int for m in ms)
        assert [m['stage'] for m in ms]==(BEGIN[:2] if fault else BEGIN+PHASES*5)
        assert ms[0]['ns']==q['start_ns'] and all(q['start_ns']<=m['ns']<=q['end_ns'] for m in ms)
        if fault:
            assert q['elapsed_ns']>=15_000_000_000 and q['exit']!=0 and q['processes_at_deadline']
        else:
            assert q['elapsed_ns']<15_000_000_000 and q['exit']==0 and not q['processes_at_deadline']
            for phase in ('peer-connect','peer-query','peer-finished'):
                assert [m['ordinal'] for m in ms if m['stage']==phase]==[1,2,3,4,5]
        processes(q['processes_at_deadline']);samples=q['samples'];assert samples
        assert q['last_sample_before_deadline']==next(v for v in reversed(samples) if v['ns']<=q['end_ns'])
        for snap in samples:
            assert set(snap)=={'ns','processes','backends'};states(snap['backends']);processes(snap['processes'])
            if not fault and snap['ns']>=ms[5]['ns']:
                assert not any(x['role']=='app' and x['state']=='idle in transaction' for x in snap['backends'])
        out[name]={'elapsed_ns':q['elapsed_ns'],'samples':len(samples),'last_stage':ms[-1]['stage'],'leftover_backends':0,'group_reaped':True}
    return out

if __name__=='__main__':print(json.dumps(check(sys.argv[1],sys.argv[2]),sort_keys=True))

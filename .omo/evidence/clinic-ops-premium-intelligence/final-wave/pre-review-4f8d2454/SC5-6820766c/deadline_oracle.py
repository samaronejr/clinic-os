"""Probe the committed success predicate on either side of its exact cutoff."""
import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from infra.posture_handoff import _assert_trace

root=Path(__file__).parent
d=json.loads((root/'handoff.json').read_bytes());assert d['sha']==os.environ['R4E_SHA']
assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==d['sha']
observed={}
for name in ('warm','fresh'):
    verdicts=[]
    for elapsed in (14_999_999_999,15_000_000_000,15_000_000_001):
        # Copy; never mutate or replace the live test's original timing receipt.
        case=dict(d['trace'][name],elapsed_ns=elapsed)
        try:_assert_trace(case)
        except AssertionError:verdicts.append(False)
        else:verdicts.append(True)
    assert verdicts==[True,False,False]
    observed[name]=verdicts
with (root/'deadline.json').open('x') as f:json.dump(dict(sha=d['sha'],run_id=d['run_id'],elapsed_ns=[14999999999,15000000000,15000000001],verdicts=observed),f,sort_keys=True)

"""Replay the stopped first r4h candidate. None of these rows certifies its successor."""
import gzip
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

RED = '9f44663bc3b2f4dd18241aae086b947663053117'
TREE = '408906b97a296b1d4168a09c5fd3dfa4bfbeed2a'
RID = '2839b83898a8428cbeedf40e779a0614'
TARGETS = {
    'tests/browser/test_browser_server_controller.py::test_controller_keeps_port_bound_through_gunicorn_handoff',
    'tests/core/test_workspace_guard_static.py::test_no_test_reaches_or_rewrites_the_refusal_guard',
}
HASHES = {
    'junit.xml':'a5e3ba6b71fc27a733a955736eae4f40e1b74dc03ff557a228cc5e6818d1533e',
    'phases.json':'41a63ccbcb68d2a48f1c70674479b8bca45548e352bd1734ab50f81ed495ba1b',
    'unit.json':'c3110f64fee054fd280ce8ac9f5e8d783c72612f9ae9f092e3cb6639cb6f4bcd',
    'reference.txt':'0b93ae926e0ec9fa60de50747f0e87503be24b73e280079f5649361f2f48157f',
    'interpreter.json':'d470a9f4821ac903a15cdb06b2b95d8acc05116435db225678203062778eb08f',
}
ID = re.compile(r'tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+(?:\[sha256-[a-f0-9]{64}\])?\Z')

def check(raw):
    assert set(raw)==set(HASHES) and all(hashlib.sha256(raw[k]).hexdigest()==v for k,v in HASHES.items())
    ref=raw['reference.txt'].decode('ascii').splitlines(); ph=json.loads(raw['phases.json']);u=json.loads(raw['unit.json'])
    assert ref==sorted(set(ref)) and len(ref)==16702 and set(ref)==set(ph)
    assert (u['run_id'],u['unit'],u['code'],u['status'],u['state'],u['result'],u['nice'])==(RID,'cpi-kek-r4h-sc5-9f44663b.service',1,1,'failed','exit-code',10)
    i=json.loads(raw['interpreter.json'])
    assert i['sha']==RED and i['run_id']==RID and i['version']=='3.12.3' and i['native_executable']=='/usr/bin/python3.12' and i['descriptor_closed']
    s,=ET.fromstring(raw['junit.xml']); assert s.get('name')==f'sc5:{RED}:{RID}' and [int(s.get(k)) for k in ('tests','failures','errors','skipped')]==[16702,2,0,0]
    assert sorted(c.get('name') for c in s)==ref
    for c in s:
        n=c.get('name');rows=ph[n];bad=n in TARGETS
        assert ID.fullmatch(n) and c.tag=='testcase' and set(c.attrib)=={'name','time'}
        assert [p[:2] for p in rows]==[['setup','passed'],['call','failed' if bad else 'passed'],['teardown','passed']]
        assert [e.tag for e in c]==(['failure'] if bad else [])
        assert all(not e.attrib and not(e.text or '').strip() and not(e.tail or '').strip() for e in c)
        t=0.0
        for _,_,v in rows:assert math.isfinite(v) and v>=0;t+=v
        assert c.get('time')==f'{t:.3f}'
    return dict(sha=RED,tree=TREE,run_id=RID,collected=16702,passed=16700,failed=2,errors=0,skipped=0,unit_exit=1,chain_exit=1,
                failed_ids=sorted(TARGETS),supplied_release_rows=0,
                attribution={'listener':'A fabricated Popen PID has no kernel process for the supervisor to pin. The corrected fixture substitutes only the workload: the real supervisor pins and reaps a private, pipe-blocked child inheriting the listener.',
                             'guard':'Three test modules imported ctypes solely as a type annotation. The unchanged refusal guard rejected those imports. The corrected tests use the actual domain slot, which encapsulates the native cell in ops.testing.pidfd_spawn.'})

def archived(d):
    d=Path(d);raw={'reference.txt':(d/'reference.txt').read_bytes()}
    for n in ('junit.xml','phases.json'):raw[n]=gzip.decompress((d/('rejected-'+n+'.gz')).read_bytes())
    for n in ('unit.json','interpreter.json'):raw[n]=(d/('rejected-'+n)).read_bytes()
    red=check(raw)
    assert red==json.loads((d/'rejected-attribution.json').read_bytes())
    cert=json.loads((d/'run.json').read_bytes()); full,=ET.parse(d/'junit.xml').iter('testsuite')
    assert cert['sha']!=RED and cert['run_id']!=RID and full.get('name')==f"sc5:{cert['sha']}:{cert['run_id']}"
    selected=[c.get('name') for c in full if c.get('name') in TARGETS and not len(c)]
    assert sorted(selected)==sorted(TARGETS)
    return red

if __name__=='__main__':print(json.dumps(archived(Path(sys.argv[1])),sort_keys=True))

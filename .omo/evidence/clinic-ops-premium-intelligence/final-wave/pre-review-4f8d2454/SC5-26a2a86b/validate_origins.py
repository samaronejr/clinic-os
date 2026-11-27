"""Audit all ten policy fields against committed PostgreSQL migration history."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

def git(*args):
    return subprocess.check_output(['git',*args])

def check(path,sha):
    data=json.loads(Path(path).read_bytes())
    assert data['sha']==sha and re.fullmatch('[0-9a-f]{32}',data['run_id'])
    observed={r['signature']:r for r in data['routines']}
    assert len(observed)==len(data['routines'])==608
    assert len(set(data['steps']))==len(data['steps'])==259
    sources=set(git('ls-tree','-r','--name-only',sha).decode().splitlines())
    prod=git('show',sha+':ops/db/routine-exceptions.tsv')
    public={}
    for l in prod.decode().splitlines():
        if l.startswith('#'):continue
        sig,args,owner,h,roles,grantable,created,named,defined,rationale=l.split('\t')
        assert sig not in public and sig in observed
        r=observed[sig]
        assert (args,owner,h,sorted(roles.split(',')),created,named,defined)==(
            r['identity_arguments'],r['owner'],r['definition_sha256'],r['grantees'],r['created'],r['named'],r['defined'])
        assert grantable=='-' and r['grant_options']==[]
        assert sig.startswith('clinic_app.') and roles and rationale.strip()
        for m in (created,named,defined):
            a,k=m.split('/')
            assert m in data['steps'] and f'apps/{a}/migrations/{k}.py' in sources
        public[sig]=rationale
    assert public.keys()==observed.keys()
    assert observed['clinic_app.intake_duplicate_transition_guard()']['created']=='intake/0017_patient_aliases'
    assert 'restore rehearsal' in public['clinic_app.tenant_decrypt(text,text,bytea)']
    return dict(routines=len(public),migrations=len(data['steps']),grant_options=0,run_id=data['run_id'],
                policy_sha256=hashlib.sha256(prod).hexdigest())

if __name__=='__main__':print(json.dumps(check(*sys.argv[1:]),sort_keys=True))

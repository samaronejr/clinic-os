"""Replay all local synthetic r4f receipts. Run in a repository with both evidence commits."""
import gzip,hashlib,json,math,re,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify,validate_aux,validate_controls,validate_handoff,validate_origins

SHA='6820766c6d02ae9baaca01e09facd0a14167ce0c'
BASE='a3013b5b2fe360e1502f459f325ceb6be0de4469'
TREE='3ff4820011cd9ff91e78401f7e3bea6c7e2356a2'
PREV='df7e65927c4dc3b4aeec1f59a551cd8218973c31'
REL='.omo/evidence/clinic-ops-premium-intelligence/final-wave/pre-review-4f8d2454/SC5-a3013b5b/'
def old(f):return subprocess.check_output(['git','show',PREV+':'+REL+f])
def load(f):return json.loads(f.read_bytes(),object_pairs_hook=verify.unique_keys)
def lifecycle(suite,ph,expected_ids):
    ids={c.get('name') for c in suite}
    assert ids==set(ph)==set(expected_ids) and len(ids)==len(suite)
    for c in suite:
        rows=ph[c.get('name')]
        assert [r[0] for r in rows]==['setup','call','teardown']
        assert all(math.isfinite(r[2]) and r[2]>=0 for r in rows)
        assert [r[1] for r in rows]==['passed','failed' if len(c) else 'passed','passed']
        elapsed=0.0
        for r in rows:elapsed+=r[2]
        assert len(c)<=1 and (not len(c) or c[0].tag=='failure') and f'{elapsed:.3f}'==c.get('time')
        assert verify.ID.fullmatch(c.get('name')) and all(not (x.text or '').strip() and not (x.tail or '').strip() for x in c.iter())
def check(d):
    d=Path(d); cert=load(d/'run.json'); gates=load(d/'gate-exits.json')
    assert (cert['sha'],cert['parent'],cert['tree'])==(SHA,BASE,TREE)
    assert subprocess.check_output(['git','rev-parse',SHA+'^',SHA+'^{tree}'],text=True).splitlines()==[BASE,TREE]
    assert set(gates)=={'static','origin','parent','positive','mutations','recorder','isolation','sc5'}
    assert all(g['sha']==SHA and g['tree']==TREE and g['exit']==0 and re.fullmatch('[0-9a-f]{32}',g['run_id']) for g in gates.values())
    assert len({g['run_id'] for g in gates.values()})==len(gates)
    for k,g in gates.items():
        assert g['unit']==f'cpi-kek-r4f-{k}-{SHA[:8]}.service'
        if k not in ('origin','static'):
            assert g['pytest_exit']==int(k in ('parent','mutations','recorder'))
    assert gates['static']['files']==2958 and gates['static']['guards']==126
    seal=verify.replay(d);rid=gates['sc5']['run_id']
    assert (seal['run_id'],seal['sha'],seal['count'])==(rid,SHA,16677)
    u=cert['manager'];assert (u['run_id'],u['unit'],u['nice'],u['result'],u['code'],u['status'],u['state'])==(rid,gates['sc5']['unit'],10,'success',1,0,'inactive') and u['start_us']<u['exit_us']
    refs=(d/'reference.txt').read_text().splitlines();baseline=old('reference.txt')
    assert hashlib.sha256(baseline).hexdigest()=='100f1dd9d5f66651805ecc184b607c999fc6c4b8ad4661dd20fce486d460725d'
    old_ids=set(baseline.decode('ascii').splitlines());added=set(refs)-old_ids
    assert len(old_ids)==16665 and old_ids<=set(refs) and len(added)==12
    assert sum(n.startswith('tests/infra/test_posture_group_identity.py::') for n in added)==2
    assert sum(n.startswith('tests/infra/test_object_owner_posture.py::') for n in added)==10
    interp=load(d/'interpreter.json');assert interp==dict(sha=SHA,run_id=rid,phase='pytest_sessionstart',implementation='CPython',version='3.12.3',native_executable='/usr/bin/python3.12',executable_relative_to_checkout='.venv/bin/python',pidfd_provider='posix',kernel_fd_target='anon_inode:[pidfd]',descriptor_closed=True)
    budget=load(d/'budget.json');assert budget==dict(sha=SHA,run_id=rid,controller='cpi-kek-r4f-release-6820766c.service',match=True,exclusive_acquisition_refused=True,nice=10)
    pidfd=set(json.loads(old('pidfd-native-full.json'))['affected_ids']);assert len(pidfd)==11 and pidfd<=set(refs)
    for mod in ['ops/testing/browser_server_supervisor.py','tests/browser/test_browser_server_supervisor.py','tests/renewal/test_browser_runner.py','tests/infra/test_db_posture_url.py','tests/teleconsult/test_mode_migration.py','tests/teleconsult/test_event_migration.py','ops/db/routine-exceptions.tsv']:
        ids=subprocess.check_output(['git','rev-parse',BASE+':'+mod,SHA+':'+mod],text=True).splitlines();assert ids[0]==ids[1]
    aux=validate_aux.check(d,SHA);mut=validate_controls.validate(d/'mutations.json',d/'mutation.xml',SHA)
    hand=validate_handoff.check(d,SHA);origin=validate_origins.check(d/'catalog-origins.json',SHA)
    assert aux['focused']['run_id']==hand['run_id']==gates['positive']['run_id'] and aux['recorder']['run_id']==gates['recorder']['run_id']
    assert load(d/'deadline.json')==dict(sha=SHA,run_id=hand['run_id'],elapsed_ns=[14999999999,15000000000,15000000001],verdicts={'warm':[True,False,False],'fresh':[True,False,False]})
    assert mut['run_id']==gates['mutations']['run_id'] and origin['run_id']==gates['origin']['run_id'] and mut['killed']==47 and mut['equivalent']==['r.rolsuper']
    orig_controls=json.loads(old('mutations.json'))['cases'];new_controls=load(d/'mutations.json')['cases']
    assert all(n in new_controls and new_controls[n]['control']==v['control'] for n,v in orig_controls.items())
    assert len(orig_controls)==38 and len(new_controls)==48
    prior_focus={c.get('name') for c in ET.fromstring(old('focused.xml')).iter('testcase')}
    for k,xml,expected in [('positive','focused.xml',prior_focus|added),('mutations','mutation.xml',None),('recorder','recorder.xml',None),('parent','parent.xml',None),('isolation','isolation.xml.gz',{n for n in refs if n.startswith('tests/isolation/')})]:
        raw=(d/xml).read_bytes();s,=ET.fromstring(gzip.decompress(raw) if xml.endswith('.gz') else raw)
        ph=json.loads(gzip.decompress((d/(k+'-phases.json.gz')).read_bytes()),object_pairs_hook=verify.unique_keys)
        lifecycle(s,ph,expected if expected is not None else {c.get('name') for c in s})
        assert s.get('name').endswith(':'+gates[k]['run_id']) and s.get('errors')==s.get('skipped')=='0'
        if k=='isolation':
            assert s.get('name')=='isolation:'+SHA+':'+gates[k]['run_id'] and len(s)==3308 and all(not len(c) for c in s)
            assert sum(n.startswith('tests/isolation/test_backup_restore_guard.py::') for n in ph)==6
        if k=='parent':
            p=load(d/'parent.json');assert s.get('name')=='parent:'+BASE+':'+p['run_id'] and p['run_id']==gates[k]['run_id'] and p['successor']==SHA and p['parent']==BASE
            assert len(s)==9 and s.get('failures')=='8' and p['exit']==1 and set(p['cases'])==set(ph)
            for n,v in p['cases'].items():
                schema=n.startswith('tests/infra/test_object_owner_posture.py::test_owner_without_ordinary_privilege') and v['prior_schema_guard']
                assert v['intended'] and v['outcome']==ph[n][1][1]==('passed' if schema else 'failed')
            assert sum(v['prior_schema_guard'] for v in p['cases'].values())==sum(v['signal_model'] for v in p['cases'].values())==1
    assert len(prior_focus)==74 and len(prior_focus|added)==86
    assert float(cert['coverage'][:-1])>=90 and gates['sc5']['guards']==[126,126]
    assert cert['run_id']==rid and cert['full_invocations']==1 and all(cert[k]==0 for k in ('missing','extra','nonpass','duplicates'))
    return dict(sha=SHA,tree=TREE,sc5=seal,coverage=cert['coverage'],focus=aux['focused'],sequences=7,recorder=aux['recorder'],mutations=mut,origins=origin,handoff=hand,
                previous_nodes=len(old_ids),added=len(added),native_pidfd=len(pidfd),isolation=3308,restore_guards=6,parent_new_red=8,parent_existing_schema_refusal=1)
if __name__=='__main__':print(json.dumps(check(Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent),sort_keys=True))

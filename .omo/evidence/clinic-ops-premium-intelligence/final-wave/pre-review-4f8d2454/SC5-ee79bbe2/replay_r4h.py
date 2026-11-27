"""Replay all local synthetic r4h receipts. Run in a repository with both evidence commits."""
import gzip,hashlib,json,math,re,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify,validate_aux,validate_controls,validate_handoff,validate_origins,check_source_receipt,replay_rejected_r4h

SHA='ee79bbe27c48c16b48d267272b73bd81aefe3369'
BASE='26a2a86b4873b2bcfea74ac42a9e7700a0661d14'
TREE='43d75b35dd452c5a6e6346ae9709bb46229498d0'
PREV='ca3a3392c5f7c031ae4efe4fcecf42d20c7c0a71'
REL='.omo/evidence/clinic-ops-premium-intelligence/final-wave/pre-review-4f8d2454/SC5-26a2a86b/'
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
    assert set(gates)=={'static','origin','parent','positive','mutations','recorder','compat','corrections','isolation','sc5'}
    assert all(g['sha']==SHA and g['tree']==TREE and g['exit']==0 and re.fullmatch('[0-9a-f]{32}',g['run_id']) for g in gates.values())
    assert len({g['run_id'] for g in gates.values()})==len(gates)
    for k,g in gates.items():
        assert g['unit']==f'cpi-kek-r4h-{k}-{SHA[:8]}.service'
        if k not in ('origin','static'):
            assert g['pytest_exit']==int(k in ('parent','mutations','recorder','compat'))
    assert gates['static']['files']==2968 and gates['static']['guards']==126
    seal=verify.replay(d);rid=gates['sc5']['run_id']
    assert (seal['run_id'],seal['sha'],seal['count'])==(rid,SHA,16702)
    u=cert['manager'];assert (u['run_id'],u['unit'],u['nice'],u['result'],u['code'],u['status'],u['state'])==(rid,gates['sc5']['unit'],10,'success',1,0,'inactive') and u['start_us']<u['exit_us']
    refs=(d/'reference.txt').read_text().splitlines();baseline=old('reference.txt')
    assert hashlib.sha256(baseline).hexdigest()=='d684223784e6c6ef8cd51625a9ad6d102f28ba9fc2cf8982b7aff577fc0b2b40'
    old_ids=set(baseline.decode('ascii').splitlines());added=set(refs)-old_ids
    assert len(old_ids)==16686 and old_ids<=set(refs) and len(added)==16
    assert sum(n.startswith('tests/infra/test_posture_birth_custody.py::') for n in added)==5
    assert sum(n.startswith('tests/infra/test_kek_signal_audit.py::') for n in added)==11
    interp=load(d/'interpreter.json');assert interp==dict(sha=SHA,run_id=rid,phase='pytest_sessionstart',implementation='CPython',version='3.12.3',native_executable='/usr/bin/python3.12',executable_relative_to_checkout='.venv/bin/python',pidfd_provider='posix',kernel_fd_target='anon_inode:[pidfd]',descriptor_closed=True)
    budget=load(d/'budget.json');assert budget==dict(sha=SHA,run_id=rid,controller='cpi-kek-r4h-release-ee79bbe2.service',match=True,exclusive_acquisition_refused=True,nice=10)
    historical='df7e65927c4dc3b4aeec1f59a551cd8218973c31:'+REL.rsplit('SC5-',1)[0]+'SC5-a3013b5b/pidfd-native-full.json'
    pidfd=set(json.loads(subprocess.check_output(['git','show',historical]))['affected_ids']);assert len(pidfd)==11 and pidfd<=set(refs)
    for mod in ['tests/browser/test_browser_server_supervisor.py','tests/renewal/test_browser_runner.py','tests/infra/posture_handoff.py','tests/infra/posture_observation.py','tests/infra/posture_trace_worker.py','tests/teleconsult/test_mode_migration.py','tests/teleconsult/test_event_migration.py','ops/db/routine-exceptions.tsv','ops/db/posture.py','tests/infra/test_object_owner_posture.py','tests/infra/test_routine_policy.py','tests/infra/test_role_family_posture.py']:
        ids=subprocess.check_output(['git','rev-parse',BASE+':'+mod,SHA+':'+mod],text=True).splitlines();assert ids[0]==ids[1]
    aux=validate_aux.check(d,SHA);mut=validate_controls.validate(d/'mutations.json',d/'mutation.xml',SHA)
    hand=validate_handoff.check(d,SHA);origin=validate_origins.check(d/'catalog-origins.json',SHA)
    assert aux['focused']['run_id']==hand['run_id']==gates['positive']['run_id'] and aux['recorder']['run_id']==gates['recorder']['run_id']
    assert load(d/'deadline.json')==dict(sha=SHA,run_id=hand['run_id'],elapsed_ns=[14999999999,15000000000,15000000001],verdicts={'warm':[True,False,False],'fresh':[True,False,False]})
    assert mut['run_id']==gates['mutations']['run_id'] and origin['run_id']==gates['origin']['run_id'] and mut['killed']==58 and mut['equivalent']==['r.rolsuper']
    orig_controls=json.loads(old('mutations.json'))['cases'];new_controls=load(d/'mutations.json')['cases']
    assert all(n in new_controls and new_controls[n]['control']==v['control'] for n,v in orig_controls.items())
    assert len(orig_controls)==53 and len(new_controls)==59
    prior_focus={c.get('name') for c in ET.fromstring(old('focused.xml')).iter('testcase')}
    for k,xml,expected in [('positive','focused.xml',prior_focus|added),('mutations','mutation.xml',None),('recorder','recorder.xml',None),('parent','parent.xml',None),('compat','compat.xml',None),('corrections','corrections.xml',replay_rejected_r4h.TARGETS),('isolation','isolation.xml.gz',{n for n in refs if n.startswith('tests/isolation/')})]:
        raw=(d/xml).read_bytes();s,=ET.fromstring(gzip.decompress(raw) if xml.endswith('.gz') else raw)
        ph=json.loads(gzip.decompress((d/(k+'-phases.json.gz')).read_bytes()),object_pairs_hook=verify.unique_keys)
        lifecycle(s,ph,expected if expected is not None else {c.get('name') for c in s})
        assert s.get('name').endswith(':'+gates[k]['run_id']) and s.get('errors')==s.get('skipped')=='0'
        if k=='corrections':
            assert s.get('name')=='r4h-corrections:'+SHA+':'+gates[k]['run_id'] and len(s)==2 and s.get('failures')=='0' and all(not len(c) for c in s)
        if k=='isolation':
            assert s.get('name')=='isolation:'+SHA+':'+gates[k]['run_id'] and len(s)==3308 and all(not len(c) for c in s)
            assert sum(n.startswith('tests/isolation/test_backup_restore_guard.py::') for n in ph)==6
        if k in ('positive','parent'):
            red=(k=='parent');p=load(d/('parent.json' if red else 'birth.json'))
            assert p['parent']==BASE and p['successor']==SHA and p['run_id']==gates[k]['run_id'] and p['target']==('parent' if red else 'successor') and p['exit']==int(red)
            if red:assert s.get('name')=='parent:'+BASE+':'+p['run_id'] and len(s)==6 and s.get('failures')=='6'
            prefix='tests/infra/test_posture_birth_custody.py::'
            matrix={validate_controls.public(prefix+'test_interrupted_outer_without_usable_receipt_is_already_reaped['+v+']'):v for v in ('eof','malformed','unreadable','silent-live')}
            matrix[prefix+'test_orphan_driver_timeout_uses_handle_runner']=None
            matrix['tests/infra/test_kek_signal_audit.py::test_entire_kek_lane_has_no_numeric_signal_fallback']=None
            assert set(p['cases'])==set(matrix) and set(matrix)<=set(ph)
            for n,v in matrix.items():
                q=p['cases'][n];assert q['intended'] is True and q['outcome']==ph[n][1][1]==('failed' if red else 'passed')
                assert q['observations']==({} if v is None else dict(variant=v,already_reaped=not red,foreign_signals=0,owned_pidfd_signals=int(not red)))
            if not red:
                prior=load(d/'custody.json');assert prior['successor']==SHA and prior['run_id']==gates[k]['run_id'] and prior['target']=='successor' and prior['exit']==0
                loss='tests/infra/test_posture_signal_custody.py::test_lost_number_cannot_authorize_error_cleanup'
                cases={validate_controls.public(loss+'['+v+']'):(sig,part) for v,sig,part in [('inherited-ignore','ignored',False),('inherited-default','default',False),('partial-spawn','ignored',True)]}
                assert set(prior['cases'])==set(cases)
                for n,(sig,part) in cases.items():
                    z=prior['cases'][n];assert z==dict(outcome='passed',intended=True,observations=dict(inherited=sig,default_at_spawn=True,restored=True,foreign_signals=0,pidfd_signals=1,partial_spawn=part,partial_child_reaped=part)) and ph[n][1][1]=='passed'
        if k=='compat':
            p=load(d/'receipt-compat.json');assert (p['sha'],p['run_id'],p['exit'])==(SHA,gates[k]['run_id'],1) and s.get('name')=='r4h-compat:'+SHA+':'+p['run_id']
            lost=validate_controls.public('tests/infra/test_posture_signal_custody.py::test_lost_number_cannot_authorize_error_cleanup[partial-spawn]')
            admit='tests/teleconsult/test_mode_migration.py::test_same_lease_posture_after_committed_migration'
            assert set(ph)=={lost,admit} and p['cases']=={lost:dict(outcome='passed',intended=True),admit:dict(outcome='failed',intended=True)}
            assert ph[lost][1][1]=='passed' and ph[admit][1][1]=='failed'
    assert len(prior_focus)==95 and len(prior_focus|added)==111
    census=check_source_receipt.check(d,SHA,BASE)
    rejected=replay_rejected_r4h.archived(d)
    assert cert['rejected_candidate_invocations']==[dict(sha=rejected['sha'],run_id=rejected['run_id'],result='rejected',supplied_rows=0)]
    assert cert['run_id']!=rejected['run_id'] and SHA!=rejected['sha']
    assert subprocess.check_output(['git','rev-parse',SHA+':tests/core/test_workspace_guard_static.py',BASE+':tests/core/test_workspace_guard_static.py'],text=True).splitlines()==['c1aae0759d8ace2f8ab0bd3b228606279d3f12d9']*2
    assert float(cert['coverage'][:-1])>=90 and gates['sc5']['guards']==[126,126]
    assert cert['run_id']==rid and cert['full_invocations']==1 and all(cert[k]==0 for k in ('missing','extra','nonpass','duplicates'))
    return dict(sha=SHA,tree=TREE,sc5=seal,coverage=cert['coverage'],focus=aux['focused'],sequences=7,recorder=aux['recorder'],mutations=mut,origins=origin,handoff=hand,
                previous_nodes=len(old_ids),added=len(added),native_pidfd=len(pidfd),isolation=3308,restore_guards=6,parent_new_red=6,new_regressions=16,unpublished_births_reaped=4,foreign_signals=0,strict_source_audit=census,legacy_socket_omission=dict(admission_refused=True,partial_birth_reaped=True),rejected_candidate=rejected,corrected_failures_passing=2)
if __name__=='__main__':print(json.dumps(check(Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parent),sort_keys=True))

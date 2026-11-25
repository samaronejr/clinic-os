"""Validate individual mutation diagnostics against the independent native JUnit."""
import hashlib, json, re, sys
import xml.etree.ElementTree as ET
from pathlib import Path

def public(node):
    base, sep, _=node.partition('[')
    return base if not sep else base+'[sha256-'+hashlib.sha256(node.encode()).hexdigest()+']'

def validate(record, junit, sha):
    result=json.loads(Path(record).read_bytes()); run=result['run_id']; assert re.fullmatch('[0-9a-f]{32}',run)
    assert result['sha']==sha and result['exit']==1
    suite,=ET.parse(junit).iter('testsuite'); assert suite.get('name')=='r4-mutation:'+sha+':'+run
    cases={public(k):v for k,v in result['cases'].items()}; assert len(cases)==len(suite)==31
    assert len({c.get('name') for c in suite})==31 and suite.get('errors')==suite.get('skipped')=='0'
    killed=set(); survived=set()
    for c in suite:
        v=cases[c.attrib['name']]; assert v['phase']=='call' and set(c.attrib)=={'name','time'}
        assert all(not (node.text or '').strip() and not node.attrib for node in c)
        if v['outcome']=='failed':
            assert [n.tag for n in c]==['failure'] and v['expected_failure'],v['control']; killed.add(v['control'])
        else:
            assert len(c)==0 and v['outcome']=='passed' and not v['expected_failure']; survived.add(v['control'])
    assert int(suite.get('failures'))==len(killed) and len(killed)+len(survived)==31
    required={'omit_native_memory_routine','check_extension_members_only','filter_by_app_connect','omit_context_owner_edge'}
    assert required<=killed,required-killed
    # The stats role already carries EXECUTE on two initially private PG16
    # functions. Removing its role arm alone no longer opens the gate.
    assert survived=={'r.rolsuper','pg_read_all_stats'},survived
    return {'killed':len(killed),'equivalent':sorted(survived),'requested_killed':sorted(required),'run_id':run}

if __name__=='__main__':print(json.dumps(validate(*sys.argv[1:]),sort_keys=True))

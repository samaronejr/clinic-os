"""Replay the seven real polarity witnesses and both committed-recorder mutants."""
from pathlib import Path
import json
import re
import sys
import xml.etree.ElementTree as ET

BASE = "tests/infra/test_role_family_posture.py::"
URL = "tests/infra/test_db_posture_url.py::test_db_posture_authenticates_the_supplied_app_database_url"
TRIPLE = [True, False, True]
LEGS = {
    URL: TRIPLE, BASE+"test_posture_refuses_transitive_set_only_parameter_authority":TRIPLE,
    BASE+"test_connectable_secondary_has_independent_routine_grants":[True,False,False,False,True],
    BASE+"test_machine_only_database_keeps_local_grant_in_scope":TRIPLE,
    BASE+"test_reachable_database_owner_activates_local_grant":TRIPLE,
    BASE+"test_connectable_but_uninspectable_database_refuses_posture":TRIPLE,
    BASE+"test_connection_disabled_database_is_not_skipped":TRIPLE,
}
RTESTS = {
    "tests/teleconsult/test_event_migration.py::test_event_authority_reverse_is_exact_with_populated_v2_events",
    "tests/teleconsult/test_mode_migration.py::test_populated_mode_migration_reverses_and_reapplies_exact_catalog",
}

def check(root, sha):
    result={}
    for prefix,kind in [('focused','r4d-positive'),('recorder','r4d-recorder')]:
        p=json.loads((root/(prefix+'.json')).read_bytes())
        suite,=ET.parse(root/(prefix+'.xml')).iter("testsuite")
        assert sha==p["sha"] and re.fullmatch("[0-9a-f]{32}",p["run_id"])
        assert suite.get("name")==kind+":"+sha+":"+p["run_id"]
        ids={c.get("name"):c for c in suite}; assert len(ids)==len(suite)==int(suite.get("tests"))
        assert suite.get("errors")==suite.get("skipped")=="0"
        assert all(set(c.attrib)=={"name","time"} and not (c.text or "").strip() for c in suite)
        if prefix=="focused":
            assert len(ids)==60 and int(suite.get("failures"))==p['exit']==0
            assert set(p['cases'])==set(LEGS) and set(LEGS)<=ids.keys()
            assert p["four_distinct_module_clusters"] is True
            for node,sequence in LEGS.items():
                assert not len(ids[node])
                assert p['cases'][node]=={'verdicts':sequence,'separate_cluster':True,
                                         'fixture_closed_databases':0,'shared_cluster_had_closed_database':True}
        else:
            assert set(p["cases"])==set(ids)==RTESTS and p['exit']==1 and int(suite.get('failures'))==2
            for node,d in p["cases"].items():
                assert d==dict(phase='call',outcome='failed',inserted=True,observed_committed=True,exact_final_recorder_assertion=True)
                failure,=ids[node]; assert failure.tag=='failure' and not failure.attrib and not (failure.text or '').strip()
        result[prefix]={"run_id":p["run_id"],"cases":len(ids),"failures":int(suite.get("failures"))}
    return result

if __name__=="__main__":
    print(json.dumps(check(Path(sys.argv[1]),sys.argv[2]),sort_keys=True))

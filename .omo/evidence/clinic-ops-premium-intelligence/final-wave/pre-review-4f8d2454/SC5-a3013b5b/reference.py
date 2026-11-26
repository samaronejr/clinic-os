"""Encode public IDs and prove all 16,651 pre-r4e IDs survive unchanged."""
import hashlib,re,sys
from pathlib import Path
COUNT=16665
BASE_SHA256='d54fd5a82f22fb2df01d419010ac0924a0cea6dc98378d648b2b3a39dc7772bd'
ADDED_FILES=['tests/infra/test_routine_policy.py']
ADDED_TESTS=['tests/infra/test_role_family_posture.py::test_bare_database_ownership_denied_without_delegated_routine', 'tests/infra/test_role_family_posture.py::test_independent_machine_owner_is_refused', 'tests/teleconsult/test_mode_migration.py::test_same_lease_posture_after_committed_migration']
raw=[line.rstrip('\n') for line in sys.stdin if line.startswith('tests/') and '::' in line]
assert len(raw)==len(set(raw))==COUNT
keys=[];old=[]
for node in raw:
    static,sep,_=node.partition('[')
    assert re.fullmatch(r'tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+',static)
    key=static if not sep else static+'[sha256-'+hashlib.sha256(node.encode()).hexdigest()+']'
    keys.append(key)
    if static not in ADDED_TESTS and static.split('::')[0] not in ADDED_FILES:old.append(key)
def lines(items):return ('\n'.join(sorted(items))+'\n').encode('ascii')
assert len(set(keys))==COUNT and len(old)==16651 and hashlib.sha256(lines(old)).hexdigest()==BASE_SHA256
with Path(sys.argv[1]).open('xb') as f:f.write(lines(keys))

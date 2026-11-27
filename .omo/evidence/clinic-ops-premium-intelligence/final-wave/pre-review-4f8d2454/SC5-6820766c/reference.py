"""Independent full collection; admit exactly twelve added IDs in the two new files."""
import hashlib,re,sys
from pathlib import Path
base=Path(sys.argv[2]).read_bytes()
assert hashlib.sha256(base).hexdigest()=='100f1dd9d5f66651805ecc184b607c999fc6c4b8ad4661dd20fce486d460725d'
old=base.decode('ascii').splitlines()
def public(s):
    head,sep,_=s.partition('[')
    assert re.fullmatch(r'tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+',head)
    return head if not sep else head+'[sha256-'+hashlib.sha256(s.encode()).hexdigest()+']'
nodes=[public(s.rstrip('\n')) for s in sys.stdin if s.startswith('tests/') and '::' in s]
assert len(nodes)==len(set(nodes))==16677 and len(old)==len(set(old))==16665 and set(old)<set(nodes)
ids=set(nodes)-set(old)
assert sum(n.startswith('tests/infra/test_posture_group_identity.py::') for n in ids)==2
assert sum(n.startswith('tests/infra/test_object_owner_posture.py::') for n in ids)==10
with Path(sys.argv[1]).open('xb') as out:out.write(('\n'.join(sorted(nodes))+'\n').encode('ascii'))

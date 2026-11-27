"""Independent full collection; admit exactly sixteen added IDs in the two new files."""
import hashlib,re,sys
from pathlib import Path
base=Path(sys.argv[2]).read_bytes()
assert hashlib.sha256(base).hexdigest()=='d684223784e6c6ef8cd51625a9ad6d102f28ba9fc2cf8982b7aff577fc0b2b40'
old=base.decode('ascii').splitlines()
def public(s):
    head,sep,_=s.partition('[')
    assert re.fullmatch(r'tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+',head)
    return head if not sep else head+'[sha256-'+hashlib.sha256(s.encode()).hexdigest()+']'
nodes=[public(s.rstrip('\n')) for s in sys.stdin if s.startswith('tests/') and '::' in s]
assert len(nodes)==len(set(nodes))==16702 and len(old)==len(set(old))==16686 and set(old)<set(nodes)
ids=set(nodes)-set(old)
assert sum(n.startswith('tests/infra/test_posture_birth_custody.py::') for n in ids)==5
assert sum(n.startswith('tests/infra/test_kek_signal_audit.py::') for n in ids)==11
with Path(sys.argv[1]).open('xb') as out:out.write(('\n'.join(sorted(nodes))+'\n').encode('ascii'))

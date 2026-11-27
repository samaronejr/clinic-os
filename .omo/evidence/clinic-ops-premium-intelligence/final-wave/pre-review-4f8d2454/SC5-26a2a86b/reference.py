"""Independent full collection; admit exactly nine added IDs in the two new files."""
import hashlib,re,sys
from pathlib import Path
base=Path(sys.argv[2]).read_bytes()
assert hashlib.sha256(base).hexdigest()=='02b675a289d2bc94bcda35fdc31d5f130c275281c4f125e97b202d9c5d4c2432'
old=base.decode('ascii').splitlines()
def public(s):
    head,sep,_=s.partition('[')
    assert re.fullmatch(r'tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+',head)
    return head if not sep else head+'[sha256-'+hashlib.sha256(s.encode()).hexdigest()+']'
nodes=[public(s.rstrip('\n')) for s in sys.stdin if s.startswith('tests/') and '::' in s]
assert len(nodes)==len(set(nodes))==16686 and len(old)==len(set(old))==16677 and set(old)<set(nodes)
ids=set(nodes)-set(old)
assert sum(n.startswith('tests/infra/test_posture_signal_native.py::') for n in ids)==4
assert sum(n.startswith('tests/infra/test_posture_signal_custody.py::') for n in ids)==5
with Path(sys.argv[1]).open('xb') as out:out.write(('\n'.join(sorted(nodes))+'\n').encode('ascii'))

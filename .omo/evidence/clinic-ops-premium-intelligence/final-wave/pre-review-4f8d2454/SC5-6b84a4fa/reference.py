"""Independent collect-only reference; standard pytest terminal IDs on stdin."""
import hashlib
import json
import re
import sys
from pathlib import Path

EXPECTED_RAW_FP="55ab491f6fdb810195aa009dc3d6d70b8aca1d1d68dcb6940c028b5075c1b640"
raw=[s.rstrip("\n") for s in sys.stdin if s.startswith("tests/") and "::" in s]
assert len(raw)==len(set(raw))==16632
rows=[]; baseline=[]
for node in raw:
    digest=hashlib.sha256(node.encode("utf-8")).hexdigest()
    if not node.startswith("tests/infra/test_role_family_posture.py::"):
        baseline.append(digest)
    address=node.split("[",1)[0]
    assert re.fullmatch(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+",address)
    rows.append(address if address==node else address+"[sha256-"+digest+"]")
assert len(baseline)==16618
assert hashlib.sha256(("\n".join(sorted(baseline))+"\n").encode()).hexdigest()==EXPECTED_RAW_FP
assert len(set(rows))==16632
with Path(sys.argv[1]).open("x", encoding="ascii") as f:
    f.writelines(s+"\n" for s in sorted(rows))

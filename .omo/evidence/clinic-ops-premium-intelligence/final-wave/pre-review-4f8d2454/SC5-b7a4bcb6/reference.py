"""Hash parameter values away and verify the independent R3 collect-only reference."""
import hashlib
import re
import sys
from pathlib import Path

COUNT=16642
PREVIOUS=16632
PREVIOUS_PUBLIC_SHA256="026a0bafd3d90de00aa60c2328123888a8292bf270cac9e31a2d92efca3d4e1d"
NEW={"test_grant_and_revoke_behind_admin_only_edge","test_migration_resolver_exception_is_not_an_app_exception",
     "test_connectable_secondary_has_independent_routine_grants","test_connectable_but_uninspectable_database_refuses_posture"}
raw=[s.rstrip("\n") for s in sys.stdin if s.startswith("tests/") and "::" in s]
assert len(raw)==len(set(raw))==COUNT
public=[]; old=[]
for node in raw:
    static,sep,_=node.partition("[")
    assert re.fullmatch(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+",static)
    key=static if not sep else static+"[sha256-"+hashlib.sha256(node.encode("utf-8")).hexdigest()+"]"
    public.append(key)
    if not (static.split("::")[0]=="tests/infra/test_role_family_posture.py" and static.split("::")[-1] in NEW):
        old.append(key)
def lines(ids): return ("\n".join(sorted(ids))+"\n").encode("ascii")
assert len(set(old))==len(old)==PREVIOUS and hashlib.sha256(lines(old)).hexdigest()==PREVIOUS_PUBLIC_SHA256
assert len(set(public))==COUNT
with Path(sys.argv[1]).open("xb") as dst: dst.write(lines(public))

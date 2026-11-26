"""Hash parameter values away and verify the independent R4 collect-only reference."""
import hashlib
import re
import sys
from pathlib import Path

COUNT=16651
PREVIOUS=16642
PREVIOUS_PUBLIC_SHA256="14b680470a7cc5d0798820ab032e49ef3d397677dde6bc987710cb412ccc0f6f"
NEW={"test_native_memory_routine_direct_grant_refused", "test_unlisted_restricted_routine_in_extension_schema", "test_machine_only_database_keeps_local_grant_in_scope", "test_reachable_database_owner_activates_local_grant", "test_connection_disabled_database_is_not_skipped", "test_initially_restricted_native_routine_cannot_be_opened_to_public", "test_private_migrated_helper_does_not_allow_new_runtime_grantee", "test_posture_rejects_inheritance_after_a_role_switch[replication]", "test_posture_rejects_inheritance_after_a_role_switch[scan-tables]"}
raw=[s.rstrip("\n") for s in sys.stdin if s.startswith("tests/") and "::" in s]
assert len(raw)==len(set(raw))==COUNT
public=[]; old=[]
for node in raw:
    static,sep,_=node.partition("[")
    assert re.fullmatch(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+",static)
    key=static if not sep else static+"[sha256-"+hashlib.sha256(node.encode("utf-8")).hexdigest()+"]"
    public.append(key)
    if not (static.split("::")[0]=="tests/infra/test_role_family_posture.py" and node.split("::")[-1] in NEW):
        old.append(key)
def lines(ids): return ("\n".join(sorted(ids))+"\n").encode("ascii")
assert len(set(old))==len(old)==PREVIOUS and hashlib.sha256(lines(old)).hexdigest()==PREVIOUS_PUBLIC_SHA256
assert len(set(public))==COUNT and hashlib.sha256(lines(public)).hexdigest()=='d54fd5a82f22fb2df01d419010ac0924a0cea6dc98378d648b2b3a39dc7772bd'
with Path(sys.argv[1]).open("xb") as dst: dst.write(lines(public))

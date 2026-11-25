"""Validate the complete control batch, including a redundant surviving mutant."""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

root=Path(__file__).parent
sha=sys.argv[1]
primary=not (root/"mutations.json").is_file()
r=json.loads((root/("exact.mutations.json" if primary else "mutations.json")).read_bytes())
s,=ET.parse(root/("exact.mutation.xml" if primary else "mutation.xml")).iter("testsuite")
assert s.attrib["name"] == "r3-mutation:"+sha+":"+r["run_id"]
assert r["exit"]==1 and len(r["cases"])==22
assert [s.get(k) for k in ("tests","failures","errors","skipped")]==["22","21","0","0"]
for node,case in r["cases"].items():
    assert case["phase"]=="call"
    if case["control"]=="r.rolsuper":
        assert node.endswith("[superuser]") and case["outcome"]=="passed" and not case["expected_failure"]
    else:
        assert case["outcome"]=="failed" and case["expected_failure"],case["control"]
added={"pg_signal_backend","pg_read_all_stats","r.rolcreaterole","r.rolbypassrls",
       "lo_export","pg_ls_dir","pg_stat_file","inspect_primary_only","allow_uninspectable"}
assert added <= {c["control"] for c in r["cases"].values() if c["expected_failure"]}
print("all seven added named privilege removals and both database omissions detected; "
      "21 killed total, one pre-existing redundant superuser comparison survived")

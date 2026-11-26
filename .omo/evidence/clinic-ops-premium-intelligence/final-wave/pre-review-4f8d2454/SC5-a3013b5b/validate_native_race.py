"""Keep the second red invocation intact; compare the real race across resource profiles."""
import gzip
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SHA = "a3013b5b2fe360e1502f459f325ceb6be0de4469"
RED = "71d05494cece40ecb1c455a426bf98ba"
NODE = "tests/infra/test_lifecycle_lock_races.py::test_cross_clinic_equal_canonical_email_waits_on_one_global_gate"
HASH = {"junit.xml":"cd37486051b2f802668973a3e7b01303c3bbf0983eac1fd302c128a41328eb44",
        "unit.json":"f9c6e34bb5e145239f4df905f130dba2ab9e830d663eb30cfcf13c5013830cf9",
        "phases.json":"434793b280e4779e91b830d7eff745b9fb186302c208d492c63672406e5ef7cb",
        "reference.txt":"100f1dd9d5f66651805ecc184b607c999fc6c4b8ad4661dd20fce486d460725d"}

def rejected(raw):
    for k,digest in HASH.items():
        assert hashlib.sha256(raw[k]).hexdigest() == digest
    ref=raw["reference.txt"].decode("ascii").splitlines()
    ph=json.loads(raw["phases.json"]); unit=json.loads(raw["unit.json"])
    assert ref==sorted(set(ref)) and set(ph)==set(ref) and len(ref)==16665
    assert unit["unit"]=="cpi-kek-r4e-full-native-a3013b5b.service" and unit["run_id"]==RED
    assert (unit["status"],unit["code"],unit["result"],unit["nice"],unit["state"])==(1,1,"exit-code",10,"failed")
    s,=ET.fromstring(raw["junit.xml"])
    assert s.get("name")==f"sc5:{SHA}:{RED}" and [int(s.get(k)) for k in ("tests","failures","errors","skipped")]==[16665,1,0,0]
    assert len(s)==len(ref) and sorted(c.get("name") for c in s)==ref
    for c in s:
        n=c.get("name"); seq=ph[n];failed=n==NODE
        assert [x[:2] for x in seq]==[["setup","passed"],["call","failed" if failed else "passed"],["teardown","passed"]]
        assert [x.tag for x in c]==(["failure"] if failed else []) and set(c.attrib)=={"name","time"}
        assert all(not x.attrib and not(x.text or "").strip() for x in c)
        assert f"{sum(x[2] for x in seq):.3f}"==c.get("time")
    assert ph[NODE][1][2]>=10
    return dict(run_id=RED,tests=16665,passed=16664,failures=1,errors=0,skipped=0,unit_exit=1,chain_exit=1,
                failed_call_seconds=ph[NODE][1][2],failed_node=NODE,release_manifest_rows=0)

def timing(data,mode,rid,stages):
    t=json.loads(data)
    assert set(t)=={"before","after","node","phase","run_id","outcome","duration_s","at_commit_deadline"}
    assert t["node"]==NODE and t["phase"]=="call" and t["run_id"]==rid and re.fullmatch("[0-9a-f]{32}",rid)
    assert t["outcome"] in {"passed","failed"} and t["duration_s"]>0 and math.isfinite(t["duration_s"])
    assert stages[1]==["call",t["outcome"],t["duration_s"]]
    b,a=t["before"],t["after"]
    assert set(a)==set(b)=={"ns","cpu_max","cpu_us","throttled_us","throttled_periods","pressure_us","major_faults"}
    assert a["ns"]>b["ns"] and a["cpu_max"]==b["cpu_max"] and a["cpu_max"]
    for c in ("cpu_us","throttled_us","throttled_periods","major_faults"):
        assert type(a[c])==type(b[c])==int and a[c]>=b[c]>=0
    assert set(a["pressure_us"])==set(b["pressure_us"])=={"cpu","io","memory"}
    assert all(a["pressure_us"][k]>=b["pressure_us"][k]>=0 for k in a["pressure_us"])
    quotas=[q.split() for q in a["cpu_max"]]
    if mode=="quota":
        q,p=map(int,quotas[0]);assert q*4==p
        assert a["throttled_periods"]>b["throttled_periods"] and a["throttled_us"]>b["throttled_us"]
        if t["outcome"]=="failed":
            assert t["at_commit_deadline"] and t["duration_s"]>=10
        else:
            assert not t["at_commit_deadline"]
    else:
        assert all(q=="max" and int(p)>0 for q,p in quotas) and t["outcome"]=="passed" and not t["at_commit_deadline"]
    return {"run_id":rid,"outcome":t["outcome"],"call_seconds":t["duration_s"],"at_commit_deadline":t["at_commit_deadline"],
            "throttled_us":a["throttled_us"]-b["throttled_us"],"leaf_cpu_max":a["cpu_max"][0]}

def profiles(read,ref,full_phases,full_id):
    profiles={}
    for mode in ("quota","ordinary"):
        p="race-"+mode
        s,=ET.fromstring(read(p+".xml"))
        prefix=p+":"+SHA+":"
        assert s.get("name").startswith(prefix)
        rid=s.get("name")[len(prefix):]; ph=json.loads(read(p+"-phases.json"))
        assert set(ph)=={NODE} and {c.get("name") for c in s}=={NODE} and s.get("skipped")=="0"
        v=timing(read(p+"-timing.json"),mode,rid,ph[NODE]); rc=int(read(p+"-pytest.exit").strip())
        assert int(s.get("failures"))==int(v["outcome"]=="failed")==rc
        assert all(c.tag=="testcase" and set(c.attrib)=={"name","time"} and all(x.tag in {"error","failure"} and not x.attrib and not(x.text or '').strip() for x in c) for c in s)
        if mode=="ordinary": assert len(s)==1 and not len(s[0]) and s.get("errors")=="0"
        v.update(pytest_exit=rc,errors=int(s.get("errors"))); profiles[mode]=v
    budget=json.loads(read("serial-budget.json"))
    assert budget==dict(sha=SHA,run_id=full_id,controller="cpi-kek-r4e-release-serial-a3013b5b.service",
                        same_lock_inode=True,concurrent_exclusive_lock_refused=True,nice=10,native_pidfd=True)
    assert hashlib.sha256(ref).hexdigest()==HASH["reference.txt"]
    profiles["certifying_full"]=timing(read("full-race-timing.json"),"ordinary",full_id,full_phases[NODE])
    assert len({RED,*(v["run_id"] for v in profiles.values())})==4
    return profiles

def archived(root):
    r={k:(root/("rejected-race-"+k)).read_bytes() for k in ("unit.json",)}
    for k in ("junit.xml","phases.json"):
        r[k]=gzip.decompress((root/("rejected-race-"+k+".gz")).read_bytes())
    r["reference.txt"]=(root/"reference.txt").read_bytes()
    def read(name):return (root/name).read_bytes()
    full_id=json.loads(read("run.json"))["run_id"]
    return {"rejected":rejected(r),"profiles":profiles(read,r["reference.txt"],json.loads(gzip.decompress(read("phases.json.gz"))),full_id)}

if __name__=="__main__":
    assert __debug__
    print(json.dumps(archived(Path(sys.argv[1])),sort_keys=True))

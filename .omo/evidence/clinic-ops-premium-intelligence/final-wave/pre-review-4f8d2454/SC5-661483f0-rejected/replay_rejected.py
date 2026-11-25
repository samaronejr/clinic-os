"""Rebuild the observed (not all-pass) 661483f0 ledger. Not a release gate."""
import argparse
import collections
import gzip
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SHA = "661483f07919db74a27cec36875a1c23e8bebc8e"
RUN = "36aecde2c8084367a6b47f4221757489"
ID = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+(?:\[sha256-[0-9a-f]{64}\])?\Z")


def unique(pairs):
    obj = dict(pairs)
    assert len(pairs) == len(obj)
    return obj


def replay(folder, *, write=False):
    assert __debug__
    reference = (folder/"reference.txt").read_text("ascii").splitlines()
    assert len(reference) == 16651 and reference == sorted(set(reference))
    assert all(ID.fullmatch(n) for n in reference)
    phases = json.loads(gzip.decompress((folder/"phases.json.gz").read_bytes()), object_pairs_hook=unique)
    assert set(phases) == set(reference)
    cert = json.loads((folder/"run.json").read_bytes(), object_pairs_hook=unique)
    assert (cert["sha"], cert["run_id"], cert["manager"]["status"]) == (SHA,RUN,1)
    xml = (folder/"junit.xml").read_bytes()
    assert b"<!" not in xml
    root = ET.fromstring(xml)
    assert root.tag == "testsuites" and len(root) == 1
    suite, = root
    assert suite.tag == "testsuite" and suite.get("name") == f"sc5:{SHA}:{RUN}"
    assert [suite.get(k) for k in ("tests","failures","errors","skipped")] == ["16651","7","0","0"]
    assert all(not (e.text or "").strip() and not (e.tail or "").strip() for e in root.iter())
    rows = []
    for case in suite:
        assert case.tag == "testcase" and set(case.attrib) == {"name","time"}
        node, duration = case.attrib["name"], case.attrib["time"]
        assert node in phases and re.fullmatch(r"\d+\.\d{3}", duration)
        assert [e.tag for e in case] in ([], ["failure"])
        assert all(not e.attrib and not len(e) for e in case)
        outcome = "failed" if len(case) else "passed"
        witness = phases[node]
        assert [p[:2] for p in witness] == [["setup","passed"],["call",outcome],["teardown","passed"]]
        measured = 0.0
        for p in witness:
            assert len(p) == 3 and type(p[2]) in (float,int) and math.isfinite(p[2]) and p[2] >= 0
            measured += p[2]
        assert f"{measured:.3f}" == duration
        rows.append({"node_id":node,"outcome":outcome,"duration":float(duration),"run_id":RUN,"sha":SHA})
    assert collections.Counter(r["node_id"] for r in rows) == collections.Counter(reference)
    assert collections.Counter(r["outcome"] for r in rows) == {"passed":16644,"failed":7}
    result = b"".join((json.dumps(r,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode("ascii") for r in sorted(rows,key=lambda r:r["node_id"]))
    dest = folder/"outcomes.jsonl"
    if write:
        with dest.open("xb") as out:
            out.write(result)
    assert dest.read_bytes() == result
    return dict(sha=SHA, run_id=RUN, passed=16644, failed=7, missing=0, extras=0, duplicates=0,
                errors=0, skipped=0, manifest_sha256=hashlib.sha256(result).hexdigest(),junit_sha256=hashlib.sha256(xml).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(json.dumps(replay(args.directory,write=args.write),sort_keys=True))

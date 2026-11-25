"""Replay the SC-5 manifest from the retained JUnit (Python stdlib only).

Run `python verify.py` alongside the evidence to verify, not re-execute, the
tests. `--write` is the producer's exclusive-create operation.
"""
import argparse
import collections
import decimal
import gzip
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

EXPECTED = 16642
ID = re.compile(
    r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py"
    r"(?:::[A-Za-z0-9_]+)+(?:\[sha256-[0-9a-f]{64}\])?\Z"
)


def require(ok, label):
    if not ok:
        raise ValueError(label)


def unique_keys(pairs):
    obj = dict(pairs)
    require(len(obj) == len(pairs), "duplicate JSON key")
    return obj


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def replay(directory, *, write=False):
    reference = (directory / "reference.txt").read_text(encoding="ascii").splitlines()
    require(len(reference) == EXPECTED and reference == sorted(set(reference)),
            "collection reference is incomplete or non-unique")
    require(all(ID.fullmatch(key) for key in reference), "invalid public ID")
    phases = json.loads(gzip.decompress((directory / "phases.json.gz").read_bytes()),
                        object_pairs_hook=unique_keys)
    require(set(phases) == set(reference), "phase reports differ from collection")
    data = (directory / "junit.xml").read_bytes()
    require(b"<!" not in data, "unexpected XML declaration")
    root = ET.fromstring(data)
    require(root.tag == "testsuites" and root.attrib == {"name":"pytest tests"}
            and len(root) == 1, "expected one native pytest suite")
    suite, = root
    require(suite.tag == "testsuite" and set(suite.attrib) == {
        "name","errors","failures","skipped","tests","time","timestamp","hostname"
    }, "unknown native suite metadata")
    require(suite.get("hostname") in {"localhost", "pop-os", "ubuntu", "workstation"},
            "refusing to publish unreviewed host identity")
    m = re.fullmatch(r"sc5:([0-9a-f]{40}):([0-9a-f]{32})", suite.attrib["name"])
    require(m is not None, "missing candidate/invocation binding")
    sha, run_id = m.groups()
    cert=json.loads((directory/"run.json").read_text(),object_pairs_hook=unique_keys)
    require(sha==cert["sha"] and run_id==cert["run_id"], "invocation certificate mismatch")
    require(int(suite.attrib["tests"]) == len(suite) == EXPECTED and
            all(suite.attrib[key] == "0" for key in ("errors","failures","skipped")),
            "suite is not a complete all-pass run")
    require(all(not (e.text or "").strip() and not (e.tail or "").strip() for e in root.iter()),
            "unexpected XML text")
    observed = collections.Counter()
    rows = []
    for case in suite:
        require(case.tag == "testcase" and set(case.attrib) == {"name","time"}
                and len(case) == 0, "case has a non-pass outcome or unknown payload")
        node, elapsed = case.attrib["name"], case.attrib["time"]
        require(node in phases and ID.fullmatch(node), "uncollected JUnit node")
        require(re.fullmatch(r"\d+\.\d{3}", elapsed) is not None, "invalid duration")
        observed[node] += 1
        lifecycle = phases[node]
        require(isinstance(lifecycle,list) and len(lifecycle) == 3 and
                [p[:2] for p in lifecycle] == [
                    ["setup","passed"],["call","passed"],["teardown","passed"]],
                "missing, duplicate or non-passing pytest phase")
        require(all(len(p) == 3 and type(p[2]) in (float,int) and
                    math.isfinite(p[2]) and p[2] >= 0 for p in lifecycle),
                "invalid phase duration")
        # Match pytest's reporter.duration += report.duration, including the
        # exact operation order at millisecond rounding boundaries.
        measured = 0.0
        for _, _, delta in lifecycle:
            measured += delta
        require(f"{measured:.3f}" == elapsed,
                "JUnit duration does not agree with phase reports")
        rows.append({"node_id":node, "outcome":"passed", "duration":float(elapsed),
                     "run_id":run_id, "sha":sha})
    require(observed == collections.Counter(reference), "missing, duplicate or extra JUnit ID")
    generated = b"".join(
        (json.dumps(row,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode("ascii")
        for row in sorted(rows,key=lambda r:r["node_id"])
    )
    dest = directory / "outcomes.jsonl"
    if write:
        with dest.open("xb") as f:
            f.write(generated)
    require(dest.read_bytes() == generated, "manifest differs from primary evidence")
    require(all(set(json.loads(l,parse_float=decimal.Decimal)) ==
                {"node_id","outcome","duration","run_id","sha"} for l in generated.splitlines()),
            "manifest must contain exactly five fields")
    return {"count":len(rows),"run_id":run_id,"sha":sha,"manifest_sha256":sha256(generated),
            "junit_sha256":sha256(data),"reference_sha256":sha256((directory/"reference.txt").read_bytes())}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--write",action="store_true")
    p.add_argument("--directory",type=Path,default=Path(__file__).resolve().parent)
    args = p.parse_args()
    print(json.dumps(replay(args.directory,write=args.write),sort_keys=True))

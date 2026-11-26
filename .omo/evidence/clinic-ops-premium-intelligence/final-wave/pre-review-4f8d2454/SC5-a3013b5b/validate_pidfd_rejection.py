"""Read-only replay of the rejected full run and its separate interpreter control."""
import collections
import gzip
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

RED = "6e314fbb752c431f8312eca63f688966"
CONTROL = "f6580c9ad9344c20b4eb81829b6352b0"
SHA = "a3013b5b2fe360e1502f459f325ceb6be0de4469"
TREE = "f976bc8161a7ff524f5c76ddc2db6074964bf272"
ID = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+(?:\[sha256-[a-f0-9]{64}\])?\Z")


def suite(data, name, counts):
    assert b"<!" not in data
    suites = ET.fromstring(data)
    s, = suites
    assert suites.tag == "testsuites" and s.tag == "testsuite" and s.get("name") == name
    assert [int(s.get(k)) for k in ("tests", "failures", "errors", "skipped")] == counts
    assert all(not (e.text or "").strip() and not (e.tail or "").strip() for e in s.iter())
    for case in s:
        assert case.tag == "testcase" and set(case.attrib) == {"name", "time"} and ID.fullmatch(case.get("name"))
        assert len(case) <= 1 and all(n.tag in {"error","failure"} and not n.attrib and len(n) == 0 for n in case)
    return s


def check(unit_raw, xml, phases_raw, ref, control):
    for content, known in (
        (xml, "490551ec4b7c7086f0d84933a02bd387e5698ab3c9802424a36116f9ccc48892"),
        (phases_raw, "1354c1da3334902ad71dc85cd9dde5c9aa19418ea7a8e420b4edaf31af20e29d"),
        (ref, "100f1dd9d5f66651805ecc184b607c999fc6c4b8ad4661dd20fce486d460725d"),
        (unit_raw, "8851cc0c23dc39a86f1f3e63d3febec2aec2d1ac055ddc9b911ba16e7e7ac1e6"),
    ):
        assert hashlib.sha256(content).hexdigest() == known
    unit = json.loads(unit_raw)
    assert unit["run_id"] == RED and unit["unit"] == "cpi-kek-r4e-full-a3013b5b.service"
    assert (unit["result"], unit["code"], unit["status"], unit["nice"], unit["state"]) == ("exit-code", 1, 1, 10, "failed")
    names = ref.decode("ascii").splitlines()
    assert names == sorted(set(names)) and len(names) == 16665 and all(ID.fullmatch(n) for n in names)
    s = suite(xml, f"sc5:{SHA}:{RED}", [16666,10,2,0])
    recorded = collections.defaultdict(list)
    for c in s:
        recorded[c.get("name")].append(c[0].tag if len(c) else "passed")
    phases = json.loads(phases_raw)
    assert set(names) == set(phases) == set(recorded)
    bad = []
    for n, rows in phases.items():
        observed = [(p,o) for p,o,_ in rows]
        assert [p for p,_ in observed] in (["setup","call","teardown"], ["setup","teardown"])
        assert all(o in {"passed","failed"} for _, o in observed)
        expected = [("failure" if p == "call" else "error") for p,o in observed if o == "failed"]
        assert recorded[n] == (expected or ["passed"])
        if expected:
            assert n.startswith(("tests/browser/test_browser_server_supervisor.py::","tests/renewal/test_browser_runner.py::"))
            bad.append(n)
    assert len(bad) == 11 and len(names)-len(bad) == 16654
    assert collections.Counter(n for values in recorded.values() for n in values) == {"passed":16654,"failure":10,"error":2}
    c = suite(control, f"pidfd-control:{SHA}:{CONTROL}", [11,0,0,0])
    assert len(c) == len(bad) and sorted(t.get("name") for t in c if len(t) == 0) == sorted(bad)
    return dict(sha=SHA, tree=TREE, rejected_run_id=RED, rejected_unit=unit["unit"], pytest_exit=1,
                rejected_chain_exit=1, collected=16665, passed=16654, failures=10, phase_errors=2,
                distinct_affected=11, skipped=0, control_run_id=CONTROL, corrected_control_passes=11,
                rejected_python="3.12.12", corrected_python="3.12.3", shared_lockfile=True,
                attribution="Managed CPython lacked native os.pidfd_open. The process supervisor could not reap its children; child-resource warnings propagated into teardown and a later setup. All affected IDs passed on the native pidfd build. No product or test modification.",
                release_manifest_rows=0)


def archived(path):
    return check((path/"rejected-pidfd-unit.json").read_bytes(),
                 gzip.decompress((path/"rejected-pidfd-junit.xml.gz").read_bytes()),
                 gzip.decompress((path/"rejected-pidfd-phases.json.gz").read_bytes()),
                 (path/"reference.txt").read_bytes(), (path/"pidfd-control.xml").read_bytes())


def full_native(pidfd_control, red_junit, native_junit, full_junit, full_phases, interpreter, rid):
    assert re.fullmatch("[0-9a-f]{32}",rid) and rid not in {RED,CONTROL,"71d05494cece40ecb1c455a426bf98ba"}
    assert interpreter==dict(executable_relative_to_checkout=".venv/bin/python",native_executable="/usr/bin/python3.12",
          implementation="CPython",version="3.12.3",pidfd_provider="posix",kernel_fd_target="anon_inode:[pidfd]",
          descriptor_closed=True,run_id=rid,sha=SHA,phase="pytest_sessionstart")
    r=suite(red_junit,f"sc5:{SHA}:{RED}",[16666,10,2,0])
    affected={c.get("name") for c in r if len(c)}
    targeted=suite(pidfd_control,f"pidfd-control:{SHA}:{CONTROL}",[11,0,0,0])
    assert len(affected)==len(targeted)==11 and affected=={c.get("name") for c in targeted if not len(c)}
    for data,run,counts in ((native_junit,"71d05494cece40ecb1c455a426bf98ba",[16665,1,0,0]),
                            (full_junit,rid,[16665,0,0,0])):
        s=suite(data,f"sc5:{SHA}:{run}",counts)
        assert collections.Counter(c.get("name") for c in s if c.get("name") in affected and not len(c))==collections.Counter(affected)
    assert all([phase[:2] for phase in full_phases[n]]==[["setup","passed"],["call","passed"],["teardown","passed"]] for n in affected)
    return dict(affected_ids=sorted(affected),passed_once_in_certifying_full=11,unchanged_native_control_passes=11,
                also_passed_in_prior_native_full=11,interpreter=interpreter,counting_run_id=rid)


def archived_full(path):
    def read(name): return (path/name).read_bytes()
    return full_native(read("pidfd-control.xml"),gzip.decompress(read("rejected-pidfd-junit.xml.gz")),
        gzip.decompress(read("rejected-race-junit.xml.gz")),read("junit.xml"),json.loads(gzip.decompress(read("phases.json.gz"))),
        json.loads(read("interpreter.json")),json.loads(read("run.json"))["run_id"])


if __name__ == "__main__":
    assert __debug__
    assert len(sys.argv)==2 or sys.argv[2:]==["--full"]
    print(json.dumps((archived_full if len(sys.argv)==3 else archived)(Path(sys.argv[1])), sort_keys=True))

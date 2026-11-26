"""Output-only pytest plugin for a PHI-free *native* pytest JUnit file.

Do not change items, test parameters, TestReports, durations, counts or outcomes.
Strip payloads from the finalized XML elements before LogXML serializes them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import pytest
from _pytest.junitxml import bin_xml_escape, mangle_test_address, xml_key

SYMBOL = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)+\Z")
NAMES: dict[tuple[str, str, str], str] = {}
PHASES: dict[str, list[tuple[str, str, float]]] = defaultdict(list)


def public_id(raw: str) -> str:
    """Keep the static node address; never disclose even synthetic parameter data."""
    base, parameterized, _ = raw.partition("[")
    if not SYMBOL.fullmatch(base):
        raise ValueError("unexpected non-static node address")
    return base if not parameterized else base+"[sha256-"+hashlib.sha256(raw.encode()).hexdigest()+"]"


def pytest_collection_finish(session):
    for item in session.items:
        names = mangle_test_address(item.nodeid)
        key = (item.location[0], ".".join(names[:-1]), bin_xml_escape(names[-1]))
        if key in NAMES:
            raise ValueError("duplicate native JUnit test address in collection")
        NAMES[key] = public_id(item.nodeid)
    if len(set(NAMES.values())) != len(NAMES):
        raise ValueError("duplicate public node ID in collection")


def pytest_runtest_logreport(report):
    # Keep only nonidentifying test metadata; do not inspect exception text.
    PHASES[public_id(report.nodeid)].append((report.when,report.outcome,report.duration))


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    if session.config.option.collectonly:
        return
    native = session.config.stash[xml_key]
    for reporter in native.node_reporters_ordered:
        case = reporter.to_xml()
        key = tuple(case.get(k,"") for k in ("file","classname","name"))
        # A collection/internal error may have no item. Preserve its negative
        # outcome and let the independent set comparison reject the run.
        name = NAMES.get(key, "uncollected")
        case.attrib = {"name":name, "time":case.attrib["time"]}
        for node in list(case):
            if node.tag in {"properties","system-out","system-err"}:
                case.remove(node)
            elif node.tag in {"error","failure","skipped"}:
                node.clear()  # remove the payload, NEVER the outcome tag
            else:
                raise ValueError("unknown JUnit child; refusing unsafe report")
    native.global_properties.clear()
    # Native pytest retains its suite counters, timestamps, duration, serializer
    # and file; this independent lifecycle witness does not generate JUnit.
    with Path(os.environ["SC5_PHASES"]).open("x",encoding="ascii") as f:
        json.dump(PHASES,f,sort_keys=True,separators=(",",":"))

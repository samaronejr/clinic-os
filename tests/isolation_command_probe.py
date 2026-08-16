#!/usr/bin/python3
"""Record isolated-database child argv without invoking Docker or the ledger."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

report = Path(os.environ["CLINIC_ISOLATION_COMMAND_REPORT"])
arguments = sys.argv[1:]
kind = (
    "ledger" if arguments and arguments[0].endswith("isolation_ledger.py") else "docker"
)
record = {"argv": arguments, "kind": kind}
with report.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")

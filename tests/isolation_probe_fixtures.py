from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing.cgroup_probe_records import _initial_journal, _validate_completed
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    canonical_bytes,
    load_json,
    write_no_replace,
)

if TYPE_CHECKING:
    from ops.testing.cgroup_capability_probe import ProbeRequest

PROBE_PID = 2_000_000_001


def complete_probe(request: ProbeRequest) -> Path:
    path = request.attempt_root / f"execution-host-probes/{request.purpose}.json"
    if path.exists():
        return _validate_completed(path, request)
    proof, _ = load_json(request.proof_path)
    parent = Path(str(proof["cgroup_parent_path"]))
    identity = proof["cgroup_parent_identity"]
    assert isinstance(identity, dict)
    parent_identity: JsonObject = dict(identity)
    child = parent / f"clinic-os-phase1a-probe-{request.attempt_id}-{request.purpose}"
    journal = _initial_journal(request, parent, parent_identity, child)
    journal.update(
        {
            "child_device": parent_identity["device"],
            "child_inode": parent_identity["inode"],
            "expected_parent_pid": os.getpid(),
            "probe_barrier_released": True,
            "probe_pgid": PROBE_PID,
            "probe_pid": PROBE_PID,
            "probe_start_ticks": 9_999,
            "removal_kind": "rmdir",
            "removed_at_utc": journal["updated_at_utc"],
            "state": "removed",
        }
    )
    path.parent.mkdir(parents=True, mode=0o700)
    write_no_replace(path, canonical_bytes(journal), mode=MODE_IMMUTABLE)
    return path

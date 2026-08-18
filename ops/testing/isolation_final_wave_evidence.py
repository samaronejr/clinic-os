"""Authenticate predecessor evidence for a final-wave controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

import rfc8785

from ops.testing import isolation_common as c
from ops.testing import isolation_controller_kernel as kernel
from ops.testing.isolation_review_lane_record import validate_review_lane_record

if TYPE_CHECKING:
    from ops.testing.isolation_final_wave_record import FinalWaveStart


def _predecessor_evidence_sha256(request: FinalWaveStart) -> str | None:
    if (evidence := request.predecessor_evidence) is None:
        return None
    if not all(
        kernel.is_sha256(value)
        for value in (
            evidence.inputs_sha256,
            evidence.f3_manifest_sha256,
            evidence.scope_pre_sha256,
        )
    ):
        kernel.fail("final-wave predecessor digest is invalid")
    terminal_hashes: dict[str, c.JsonValue] = {}
    for lane, record in (("F1", evidence.f1_record), ("F2", evidence.f2_record)):
        try:
            validate_review_lane_record(record)
        except c.IsolationError as error:
            message = f"{lane} predecessor record is invalid"
            raise c.IsolationError(message) from error
        identity = (
            record.get("attempt_id"),
            record.get("lane"),
            record.get("sha"),
            record.get("inputs_sha256"),
            record.get("state"),
        )
        expected = (
            request.attempt_id,
            lane,
            request.sha,
            evidence.inputs_sha256,
            "success",
        )
        if identity != expected or not kernel.is_sha256(
            record.get("terminal_outputs_sha256")
        ):
            kernel.fail(f"{lane} predecessor is not a successful bound review")
        terminal_hashes[f"{lane.lower()}_terminal_outputs_sha256"] = record[
            "terminal_outputs_sha256"
        ]
    projection: c.JsonObject = {
        **terminal_hashes,
        "f3_manifest_sha256": evidence.f3_manifest_sha256,
        "inputs_sha256": evidence.inputs_sha256,
        "schema_version": 1,
        "scope_pre_sha256": evidence.scope_pre_sha256,
    }
    return c.raw_sha256(rfc8785.dumps(projection))

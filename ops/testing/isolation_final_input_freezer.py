"""Publish one immutable manifest after final input authentication."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_atomic_publication import (
    publish_immutable,
    require_published_bytes,
)
from ops.testing.isolation_candidate_records import (
    authorization_destination,
    published_observation,
    unpublished_observation,
)
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    IsolationError,
    JsonObject,
    canonical_bytes,
    directory_identity,
)
from ops.testing.isolation_final_input_auth import (
    CandidateInspection as _CandidateInspection,
)
from ops.testing.isolation_final_input_auth import (
    FinalInputFreeze as _FinalInputFreeze,
)
from ops.testing.isolation_final_input_auth import (
    SourceInspection as _SourceInspection,
)
from ops.testing.isolation_final_input_manifest import _authenticated_manifest
from ops.testing.isolation_ledger_store import LedgerSession, locked_open_ledger
from ops.testing.isolation_terminal_publisher_authorizations import (
    entry_present,
    publisher_input_state,
    validate_terminal_publisher_claim,
)

if TYPE_CHECKING:
    from pathlib import Path

SHA40: Final = re.compile(r"^[0-9a-f]{40}$")


def _freeze_final_inputs(request: _FinalInputFreeze) -> Path:
    _validate_request(request)
    with locked_open_ledger(request.ledger_path) as session:
        manifest, publisher = _authenticated_manifest(request, session)
        _publish_manifest(session, publisher, (canonical_bytes(manifest), request))
    return request.output_path


def _validate_request(request: _FinalInputFreeze) -> None:
    roots = (
        request.worktree,
        request.attempt_root,
        request.staging_root,
        request.output_path,
        request.codex_source_path,
        request.uv_source_path,
        request.final_suite_path,
    )
    if not all(path.is_absolute() for path in roots):
        _fail("final input paths must be absolute")
    if request.staging_root.parent != request.attempt_root / "claims":
        _fail("final input staging root is not claim-owned")
    expected_output = (
        request.attempt_root.parents[1]
        / "clinic-os-phase1a-final"
        / "terminal"
        / "inputs.json"
    )
    if (
        request.attempt_root.name != request.attempt_id
        or request.attempt_root.parent.name != "clinic-os-phase1a-runtime"
        or request.output_path != expected_output
    ):
        _fail("final input destination is not the fixed terminal path")
    try:
        for path in (
            request.attempt_root,
            request.staging_root,
            request.output_path.parent,
        ):
            if directory_identity(path).get("mode") != MODE_DIRECTORY:
                _fail("final input directory is not private")
    except FileNotFoundError as error:
        message = "final input publisher directory is absent"
        raise IsolationError(message) from error
    inspected = request.source_inspection
    if (
        SHA40.fullmatch(request.sha) is None
        or SHA40.fullmatch(request.tree_sha) is None
        or (inspected.sha, inspected.tree_sha, inspected.clean)
        != (request.sha, request.tree_sha, True)
    ):
        _fail("final input source inspection drifted")


def _publish_manifest(
    session: LedgerSession,
    publisher: JsonObject,
    publication: tuple[bytes, _FinalInputFreeze],
) -> None:
    raw, request = publication
    authorization, observation = publisher_input_state(publisher)
    destination = authorization_destination(authorization)
    if destination != request.output_path:
        _fail("terminal publisher input destination drifted")
    expected = published_observation(authorization, raw)
    if observation.get("status") == "published":
        if observation != expected:
            _fail("terminal publisher input observation drifted")
        _ = validate_terminal_publisher_claim(publisher, destination.parent)
        require_published_bytes(destination, raw)
        return
    if observation != unpublished_observation(authorization):
        _fail("terminal publisher input observation is not unpublished")
    if not entry_present(destination):
        _ = validate_terminal_publisher_claim(publisher, destination.parent)
    publish_manifest_bytes(destination, raw, request.attempt_id)
    projected = copy.deepcopy(publisher)
    _authorization, projected_observation = publisher_input_state(projected)
    projected_observation.clear()
    projected_observation.update(expected)
    _ = validate_terminal_publisher_claim(projected, destination.parent)
    observation.clear()
    observation.update(expected)
    session.commit()
    _ = validate_terminal_publisher_claim(publisher, destination.parent)


def _fail(message: str) -> Never:
    raise IsolationError(message)


CandidateInspection = _CandidateInspection
SourceInspection = _SourceInspection
FinalInputFreeze = _FinalInputFreeze
freeze_final_inputs = _freeze_final_inputs
publish_manifest_bytes = publish_immutable

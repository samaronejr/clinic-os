"""Closed causal stage graph for the durable browser-server session.

Every externally observable step of the browser-server session is one stage in
this graph. A stage may only be recorded once every one of its predecessors is
already recorded, so the journal itself is the causal proof of the plan's
ordering contract rather than a comment about it.
"""

from __future__ import annotations

from typing import Final, Never

LEDGER_REFRESHED: Final = "ledger-refreshed"
MATERIALIZER_ACTIVE: Final = "materializer-active"
DB_ACTIVE: Final = "db-active"
OWNER_RELEASE: Final = "owner-release"
OWNER_BOOTSTRAP: Final = "owner-bootstrap"
CA_EXPORTED: Final = "ca-exported"
SETTINGS_IMPORTED: Final = "settings-imported"
MASTER_BOUND: Final = "master-bound"
APP_ACTIVE: Final = "app-active"
CANDIDATE_PROBED: Final = "candidate-probed"
RUNNER_INTENT: Final = "runner-intent"
RUNNER_CREATED: Final = "runner-created"
RUNNER_INSPECTED: Final = "runner-inspected"
RUNNER_PREPARED: Final = "runner-prepared"
RUNNER_STARTED: Final = "runner-started"
RUNNER_ATTESTED: Final = "runner-attested"
RUNNER_ACTIVE: Final = "runner-active"
OWNER_START_SENT: Final = "owner-start-sent"
OWNER_PENDING_READY: Final = "owner-pending-ready"
OWNER_HELPER_PENDING: Final = "owner-helper-pending"
OWNER_CONFIRMED: Final = "owner-confirmed"
ADMIN_PROVISIONED: Final = "admin-provisioned"
RECEPTIONIST_PROVISIONED: Final = "receptionist-provisioned"
PHYSICIAN_PROVISIONED: Final = "physician-provisioned"
ADMIN_START_SENT: Final = "admin-start-sent"
ADMIN_PENDING_READY: Final = "admin-pending-ready"
ADMIN_CONFIRMED: Final = "admin-confirmed"
PHYSICIAN_START_SENT: Final = "physician-start-sent"
PHYSICIAN_PENDING_READY: Final = "physician-pending-ready"
PHYSICIAN_CONFIRMED: Final = "physician-confirmed"
RECEPTIONIST_LOGGED_IN: Final = "receptionist-logged-in"
SUITE_AUTHORIZED: Final = "suite-authorized"
SUITE_DISPATCHED: Final = "suite-dispatched"
ARTIFACTS_FRAMED: Final = "artifacts-framed"
ARTIFACT_PUBLISHED: Final = "artifact-published"
PUBLICATION_ACKNOWLEDGED: Final = "publication-acknowledged"
RUNNER_REMOVE_INTENT: Final = "runner-remove-intent"
RUNNER_REMOVED: Final = "runner-removed"
MASTER_TERMINATED: Final = "master-terminated"
DB_RELEASED: Final = "db-released"
SEALED: Final = "sealed"

PREDECESSORS: Final[dict[str, tuple[str, ...]]] = {
    LEDGER_REFRESHED: (),
    MATERIALIZER_ACTIVE: (LEDGER_REFRESHED,),
    DB_ACTIVE: (MATERIALIZER_ACTIVE,),
    OWNER_RELEASE: (DB_ACTIVE,),
    OWNER_BOOTSTRAP: (OWNER_RELEASE,),
    CA_EXPORTED: (OWNER_BOOTSTRAP,),
    SETTINGS_IMPORTED: (CA_EXPORTED,),
    MASTER_BOUND: (SETTINGS_IMPORTED,),
    APP_ACTIVE: (MASTER_BOUND,),
    CANDIDATE_PROBED: (APP_ACTIVE,),
    RUNNER_INTENT: (CANDIDATE_PROBED,),
    RUNNER_CREATED: (RUNNER_INTENT,),
    RUNNER_INSPECTED: (RUNNER_CREATED,),
    RUNNER_PREPARED: (RUNNER_INSPECTED,),
    RUNNER_STARTED: (RUNNER_PREPARED,),
    RUNNER_ATTESTED: (RUNNER_STARTED,),
    RUNNER_ACTIVE: (RUNNER_ATTESTED,),
    OWNER_START_SENT: (RUNNER_ACTIVE,),
    OWNER_PENDING_READY: (OWNER_START_SENT,),
    OWNER_HELPER_PENDING: (OWNER_PENDING_READY,),
    OWNER_CONFIRMED: (OWNER_HELPER_PENDING,),
    ADMIN_PROVISIONED: (OWNER_CONFIRMED,),
    RECEPTIONIST_PROVISIONED: (ADMIN_PROVISIONED,),
    PHYSICIAN_PROVISIONED: (RECEPTIONIST_PROVISIONED,),
    ADMIN_START_SENT: (ADMIN_PROVISIONED,),
    ADMIN_PENDING_READY: (ADMIN_START_SENT,),
    ADMIN_CONFIRMED: (ADMIN_PENDING_READY,),
    PHYSICIAN_START_SENT: (PHYSICIAN_PROVISIONED,),
    PHYSICIAN_PENDING_READY: (PHYSICIAN_START_SENT,),
    PHYSICIAN_CONFIRMED: (PHYSICIAN_PENDING_READY,),
    RECEPTIONIST_LOGGED_IN: (RECEPTIONIST_PROVISIONED,),
    SUITE_AUTHORIZED: (
        ADMIN_CONFIRMED,
        PHYSICIAN_CONFIRMED,
        RECEPTIONIST_LOGGED_IN,
    ),
    SUITE_DISPATCHED: (SUITE_AUTHORIZED,),
    ARTIFACTS_FRAMED: (SUITE_DISPATCHED,),
    ARTIFACT_PUBLISHED: (ARTIFACTS_FRAMED,),
    PUBLICATION_ACKNOWLEDGED: (ARTIFACT_PUBLISHED,),
    RUNNER_REMOVE_INTENT: (PUBLICATION_ACKNOWLEDGED,),
    RUNNER_REMOVED: (RUNNER_REMOVE_INTENT,),
    MASTER_TERMINATED: (RUNNER_REMOVED,),
    DB_RELEASED: (MASTER_TERMINATED,),
    SEALED: (DB_RELEASED,),
}
STAGES: Final = tuple(PREDECESSORS)
TERMINAL_STAGE: Final = SEALED
CANONICAL_ORDER: Final = STAGES


class BrowserStageError(RuntimeError):
    """Reject a stage recorded out of its proven causal order."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying causal failure."""
        super().__init__(f"browser stage rejected: {reason}")


def _fail(reason: str) -> Never:
    raise BrowserStageError(reason)


def require_known_stage(stage: str) -> str:
    """Return the stage only when it belongs to the closed vocabulary."""
    if stage not in PREDECESSORS:
        _fail(f"{stage} is not a known browser-server stage")
    return stage


def missing_predecessors(stage: str, recorded: tuple[str, ...]) -> tuple[str, ...]:
    """Return the predecessors this stage still requires, in canonical order."""
    require_known_stage(stage)
    seen = set(recorded)
    return tuple(item for item in PREDECESSORS[stage] if item not in seen)


def require_recordable(stage: str, recorded: tuple[str, ...]) -> None:
    """Reject repeats and every stage whose predecessors are incomplete."""
    require_known_stage(stage)
    if stage in recorded:
        _fail(f"{stage} was already recorded")
    missing = missing_predecessors(stage, recorded)
    if missing:
        _fail(f"{stage} requires {', '.join(missing)} first")


def is_complete(recorded: tuple[str, ...]) -> bool:
    """Report whether every stage in the closed graph has been recorded."""
    return set(recorded) == set(PREDECESSORS)

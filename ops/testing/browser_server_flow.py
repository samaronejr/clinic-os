"""Causally ordered browser-server session flow over injected effects.

Every external action is one method on `BrowserSessionEffects`, so the ordering
contract lives here and is identical whether the effects are the real ledger,
Docker, Gunicorn, and pseudo-terminals or a recording double in a test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing import browser_server_stages as stage
from ops.testing.browser_artifact_publisher import (
    artifact_prefix,
    frame_suite_artifacts,
    manifest_digest,
    require_acknowledged,
)
from ops.testing.browser_runner_contract import selected_suites
from ops.testing.browser_secret_channel import (
    ADMIN_CONFIRM,
    ADMIN_START,
    OWNER_CONFIRM,
    OWNER_START,
    PHYSICIAN_CONFIRM,
    PHYSICIAN_START,
    RECEPTIONIST_LOGIN,
)
from ops.testing.browser_totp_helpers import (
    CLINIC_ADMIN,
    CONFIRMED,
    OWNER,
    PENDING,
    PHYSICIAN,
    HelperSequence,
)

if TYPE_CHECKING:
    from ops.testing.browser_server_journal import BarrierJournal
    from ops.testing.browser_session_effects import BrowserSessionEffects

REQUIRED_SUITE: Final = "patient"
DEFAULT_REQUIRED: Final = (REQUIRED_SUITE,)
RUNNER_START_DEADLINE_SECONDS: Final = 5.0
EXPECTED_WORKERS: Final = 2
CREATED_STATE: Final = "created"
PROVISION_ORDER: Final = (CLINIC_ADMIN, "receptionist", PHYSICIAN)
PROVISION_STAGES: Final = {
    CLINIC_ADMIN: stage.ADMIN_PROVISIONED,
    "receptionist": stage.RECEPTIONIST_PROVISIONED,
    PHYSICIAN: stage.PHYSICIAN_PROVISIONED,
}
ENROLL_FRAMES: Final = {
    CLINIC_ADMIN: (ADMIN_START, ADMIN_CONFIRM),
    PHYSICIAN: (PHYSICIAN_START, PHYSICIAN_CONFIRM),
}
ENROLL_STAGES: Final = {
    CLINIC_ADMIN: (
        stage.ADMIN_START_SENT,
        stage.ADMIN_PENDING_READY,
        stage.ADMIN_CONFIRMED,
    ),
    PHYSICIAN: (
        stage.PHYSICIAN_START_SENT,
        stage.PHYSICIAN_PENDING_READY,
        stage.PHYSICIAN_CONFIRMED,
    ),
}


class BrowserFlowError(RuntimeError):
    """Reject an external observation that breaks the session contract."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying flow failure."""
        super().__init__(f"browser session flow rejected: {reason}")


def _fail(reason: str) -> Never:
    raise BrowserFlowError(reason)


def _bring_up_application(
    effects: BrowserSessionEffects, journal: BarrierJournal
) -> None:
    effects.refresh_ledger()
    journal.record(stage.LEDGER_REFRESHED)
    effects.activate_materializer()
    journal.record(stage.MATERIALIZER_ACTIVE)
    effects.activate_database()
    journal.record(stage.DB_ACTIVE)
    effects.run_owner_release()
    journal.record(stage.OWNER_RELEASE)
    effects.bootstrap_owner()
    journal.record(stage.OWNER_BOOTSTRAP)
    effects.export_ca()
    journal.record(stage.CA_EXPORTED)
    port = effects.reserve_process_claim()
    journal.record(stage.SETTINGS_IMPORTED)
    effects.request_start_master(port)
    journal.record(stage.MASTER_BOUND)
    workers = effects.observe_workers()
    if workers != EXPECTED_WORKERS:
        _fail(f"expected {EXPECTED_WORKERS} workers but observed {workers}")
    journal.record(stage.APP_ACTIVE)


def _bring_up_runner(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
    required: tuple[str, ...],
) -> None:
    available = effects.probe_candidate()
    expected = sorted(required)
    if available != expected:
        _fail(f"candidate advertises {available} rather than {expected}")
    selected_suites(available, expected)
    journal.record(stage.CANDIDATE_PROBED)
    if not effects.runner_intent():
        _fail("runner intent was not durably recorded before creation")
    journal.record(stage.RUNNER_INTENT)
    effects.runner_create()
    journal.record(stage.RUNNER_CREATED)
    observed = effects.runner_inspect()
    if observed != CREATED_STATE:
        _fail(f"runner was inspected in {observed} rather than created state")
    journal.record(stage.RUNNER_INSPECTED)
    effects.runner_prepare()
    journal.record(stage.RUNNER_PREPARED)
    elapsed = effects.runner_start()
    if elapsed > RUNNER_START_DEADLINE_SECONDS:
        _fail("runner did not start within five seconds")
    journal.record(stage.RUNNER_STARTED)
    if not effects.runner_attest():
        _fail("runner produced no bootstrap attestation")
    journal.record(stage.RUNNER_ATTESTED)
    effects.runner_activate()
    journal.record(stage.RUNNER_ACTIVE)


def _enroll_owner(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
    helpers: HelperSequence,
) -> None:
    effects.send_password_frame(OWNER_START, OWNER)
    journal.record(stage.OWNER_START_SENT)
    effects.await_pending_ready(OWNER)
    journal.record(stage.OWNER_PENDING_READY)
    _, code = effects.run_helper(OWNER, PENDING)
    journal.record(stage.OWNER_HELPER_PENDING)
    effects.send_code_frame(OWNER_CONFIRM, code)
    journal.record(stage.OWNER_CONFIRMED)
    del helpers


def _provision_personas(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
) -> None:
    for persona in PROVISION_ORDER:
        _, code = effects.run_helper(OWNER, CONFIRMED)
        effects.provision_staff(persona, code)
        journal.record(PROVISION_STAGES[persona])


def _enroll_privileged(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
) -> None:
    for persona in (CLINIC_ADMIN, PHYSICIAN):
        start_frame, confirm_frame = ENROLL_FRAMES[persona]
        sent, ready, confirmed = ENROLL_STAGES[persona]
        effects.send_password_frame(start_frame, persona)
        journal.record(sent)
        effects.await_pending_ready(persona)
        journal.record(ready)
        _, code = effects.run_helper(persona, PENDING)
        effects.send_code_frame(confirm_frame, code)
        journal.record(confirmed)


def _tear_down(effects: BrowserSessionEffects, journal: BarrierJournal) -> None:
    effects.runner_remove()
    journal.record(stage.RUNNER_REMOVE_INTENT)
    journal.record(stage.RUNNER_REMOVED)
    effects.stop_master()
    journal.record(stage.MASTER_TERMINATED)
    effects.release_database()
    journal.record(stage.DB_RELEASED)
    journal.record(stage.SEALED)


def _export_artifacts(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
    suite_id: str,
) -> dict[str, bytes]:
    frame = effects.authorize_suite(suite_id)
    if not effects.verify_authorization(suite_id, frame):
        _fail(f"{suite_id} dispatch frame is not authenticated for this session")
    journal.record(stage.SUITE_AUTHORIZED)
    artifacts = effects.dispatch_suite(suite_id)
    if not artifacts:
        _fail("suite dispatch produced no artifact")
    journal.record(stage.SUITE_DISPATCHED)
    frames = frame_suite_artifacts(suite_id, artifacts)
    journal.record(stage.ARTIFACTS_FRAMED)
    published = effects.publish_frames(frames)
    prefix = artifact_prefix(suite_id)
    if sorted(published) != sorted(artifacts) or any(
        not item.startswith(prefix) for item in published
    ):
        _fail(f"host published {sorted(published)} rather than the {suite_id} set")
    journal.record(stage.ARTIFACT_PUBLISHED)
    digest = manifest_digest(frames)
    require_acknowledged(
        effects.acknowledge_publication(suite_id, digest), suite_id, digest
    )
    journal.record(stage.PUBLICATION_ACKNOWLEDGED)
    return artifacts


def run_session(
    effects: BrowserSessionEffects,
    journal: BarrierJournal,
    helpers: HelperSequence,
    required: tuple[str, ...] = DEFAULT_REQUIRED,
    suite_id: str = REQUIRED_SUITE,
) -> dict[str, bytes]:
    """Execute the whole causally ordered session and return its artifacts."""
    if suite_id not in required:
        _fail(f"{suite_id} is not part of the required suite set")
    _bring_up_application(effects, journal)
    _bring_up_runner(effects, journal, required)
    _enroll_owner(effects, journal, helpers)
    _provision_personas(effects, journal)
    _enroll_privileged(effects, journal)
    effects.send_password_frame(RECEPTIONIST_LOGIN, "receptionist")
    journal.record(stage.RECEPTIONIST_LOGGED_IN)
    helpers.require_complete()
    artifacts = _export_artifacts(effects, journal, suite_id)
    _tear_down(effects, journal)
    return artifacts

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from ops.testing import browser_server_stages as stage
from ops.testing.browser_secret_channel import FRAME_COUNT, SecretChannel
from ops.testing.browser_server_flow import run_session
from ops.testing.browser_server_journal import BarrierJournal
from ops.testing.browser_totp_helpers import HelperSequence

from browser.browser_server_fakes import RecordingEffects

if TYPE_CHECKING:
    from pathlib import Path


CAUSAL_CHAIN: Final = (
    stage.LEDGER_REFRESHED,
    stage.MATERIALIZER_ACTIVE,
    stage.DB_ACTIVE,
    stage.OWNER_RELEASE,
    stage.OWNER_BOOTSTRAP,
    stage.CA_EXPORTED,
    stage.SETTINGS_IMPORTED,
    stage.MASTER_BOUND,
    stage.APP_ACTIVE,
    stage.CANDIDATE_PROBED,
    stage.RUNNER_INTENT,
    stage.RUNNER_CREATED,
    stage.RUNNER_INSPECTED,
    stage.RUNNER_PREPARED,
    stage.RUNNER_STARTED,
    stage.RUNNER_ATTESTED,
    stage.RUNNER_ACTIVE,
    stage.OWNER_START_SENT,
    stage.OWNER_PENDING_READY,
    stage.OWNER_HELPER_PENDING,
    stage.OWNER_CONFIRMED,
    stage.ADMIN_PROVISIONED,
    stage.RECEPTIONIST_PROVISIONED,
    stage.PHYSICIAN_PROVISIONED,
    stage.ADMIN_START_SENT,
    stage.ADMIN_PENDING_READY,
    stage.ADMIN_CONFIRMED,
    stage.PHYSICIAN_START_SENT,
    stage.PHYSICIAN_PENDING_READY,
    stage.PHYSICIAN_CONFIRMED,
    stage.RECEPTIONIST_LOGGED_IN,
    stage.SUITE_AUTHORIZED,
    stage.SUITE_DISPATCHED,
    stage.ARTIFACTS_FRAMED,
    stage.ARTIFACT_PUBLISHED,
    stage.PUBLICATION_ACKNOWLEDGED,
    stage.RUNNER_REMOVE_INTENT,
    stage.RUNNER_REMOVED,
    stage.MASTER_TERMINATED,
    stage.DB_RELEASED,
    stage.SEALED,
)


def run_recorded_session(
    tmp_path: Path,
    faults: frozenset[str] = frozenset(),
    required: tuple[str, ...] = ("patient",),
    suite_id: str = "patient",
) -> tuple[
    BarrierJournal,
    RecordingEffects,
    int,
]:
    read_fd, write_fd = os.pipe()
    channel = SecretChannel(write_fd)
    helpers = HelperSequence()
    effects = RecordingEffects(channel, helpers, faults, required)
    journal = BarrierJournal(tmp_path / "session.jsonl")
    try:
        run_session(effects, journal, helpers, required, suite_id)
    finally:
        if len(channel.sent) == FRAME_COUNT:
            channel.close()
        else:
            os.close(write_fd)
    return journal, effects, read_fd

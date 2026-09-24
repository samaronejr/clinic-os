from __future__ import annotations

import os
import pickle
import socket
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import browser_server_stages as stage
from ops.testing.browser_artifact_publisher import ArtifactPublicationError
from ops.testing.browser_server_flow import BrowserFlowError
from ops.testing.browser_server_journal import BarrierJournal, BrowserJournalError
from ops.testing.browser_server_stages import BrowserStageError
from ops.testing.browser_suites.patient import build_patient_suite
from ops.testing.browser_supervisor_session import (
    BrowserSupervisorSession,
    ClinicBrowserContexts,
    RunnerSuiteRegistry,
    SupervisorSessionError,
    authenticate_dispatch,
    dispatch_frame,
)
from ops.testing.browser_totp_helpers import (
    CLINIC_ADMIN,
    OWNER,
    PHYSICIAN,
)

from browser.browser_session_harness import CAUSAL_CHAIN, run_recorded_session

if TYPE_CHECKING:
    from pathlib import Path

CLAIM: Final = "6889f2de-b9ac-4ac6-bb78-136e54a4e340"


def test_full_causal_chain_is_recorded_in_exact_order(tmp_path: Path) -> None:
    journal, effects, read_fd = run_recorded_session(tmp_path)

    assert journal.recorded == CAUSAL_CHAIN
    assert journal.sealed
    assert effects.provisioned == [CLINIC_ADMIN, "receptionist", PHYSICIAN]
    assert effects.pending_ready == [OWNER, CLINIC_ADMIN, PHYSICIAN]
    os.close(read_fd)


@pytest.mark.parametrize(
    ("fault", "match"),
    [
        ("workers", "workers"),
        ("suite-ids", "advertises"),
        ("forged-frame", "not authenticated"),
        ("publish-drop", "rather than the patient set"),
        ("no-intent", "runner intent"),
        ("runner-state", "created state"),
        ("runner-slow", "five seconds"),
        ("no-attest", "bootstrap attestation"),
        ("no-artifacts", "no artifact"),
    ],
)
def test_each_contract_violation_aborts_the_session(
    tmp_path: Path,
    fault: str,
    match: str,
) -> None:
    with pytest.raises(BrowserFlowError, match=match):
        run_recorded_session(tmp_path, frozenset({fault}))


def test_stage_graph_rejects_every_out_of_order_record() -> None:
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.OWNER_RELEASE, (stage.LEDGER_REFRESHED,))
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.OWNER_BOOTSTRAP, ())
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.RUNNER_ACTIVE, (stage.RUNNER_STARTED,))
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.OWNER_START_SENT, (stage.APP_ACTIVE,))
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.ADMIN_PROVISIONED, (stage.RUNNER_ACTIVE,))
    with pytest.raises(BrowserStageError, match="requires"):
        stage.require_recordable(stage.OWNER_HELPER_PENDING, (stage.OWNER_START_SENT,))
    with pytest.raises(BrowserStageError, match="not a known"):
        stage.require_recordable("invented-stage", ())


def test_journal_rejects_repeats_and_premature_seal(tmp_path: Path) -> None:
    journal = BarrierJournal(tmp_path / "j.jsonl")
    journal.record(stage.LEDGER_REFRESHED)
    with pytest.raises(BrowserStageError, match="already recorded"):
        journal.record(stage.LEDGER_REFRESHED)
    with pytest.raises(BrowserStageError, match="requires"):
        journal.record(stage.SEALED)


def test_journal_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    journal = BarrierJournal(path)
    journal.record(stage.LEDGER_REFRESHED)
    journal.record(stage.MATERIALIZER_ACTIVE)
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n" + lines[1].replace("materializer", "db") + "\n")

    with pytest.raises(BrowserJournalError):
        BarrierJournal(path)


def test_dispatch_frame_is_bound_to_claim_sequence_suite_and_argv() -> None:
    argv = ["--require-suite", "patient"]
    raw = dispatch_frame(CLAIM, 0, "patient", argv)
    authenticate_dispatch(raw, CLAIM, 0, "patient", argv)
    for claim, sequence, suite, other in (
        ("0" * 8 + "-0000-0000-0000-" + "0" * 12, 0, "patient", argv),
        (CLAIM, 1, "patient", argv),
        (CLAIM, 0, "patient", ["--require-suite", "availability"]),
    ):
        with pytest.raises(SupervisorSessionError, match="not authenticated"):
            authenticate_dispatch(raw, claim, sequence, suite, other)


def test_retained_contexts_are_never_serialized() -> None:
    contexts = ClinicBrowserContexts()
    contexts.retain("receptionist", object())

    assert contexts.personas == ("receptionist",)
    with pytest.raises(SupervisorSessionError, match="never serialized"):
        pickle.dumps(contexts)
    with pytest.raises(SupervisorSessionError, match="already retained"):
        contexts.retain("receptionist", object())
    with pytest.raises(SupervisorSessionError, match="not a known browser persona"):
        contexts.retain("intruder", object())
    with pytest.raises(SupervisorSessionError, match="not retained"):
        contexts.borrow("owner")


def test_suite_registry_only_accepts_allowlisted_runner_entrypoints() -> None:
    registry = RunnerSuiteRegistry()
    entrypoint = build_patient_suite(
        {
            "base_url": "http://127.0.0.1:9",
            "clinic_id": CLAIM,
            "password": "synthetic-secret",
            "username": "synthetic.receptionist",
        }
    )
    registry.register("patient", entrypoint)

    assert registry.resolve("patient") is entrypoint
    with pytest.raises(SupervisorSessionError, match="already registered"):
        registry.register("patient", entrypoint)
    with pytest.raises(SupervisorSessionError, match="not an allowlisted"):
        RunnerSuiteRegistry().register("patient", os.getcwd)
    with pytest.raises(SupervisorSessionError, match="not callable"):
        RunnerSuiteRegistry().register("patient", "os.system")


def test_supervisor_session_binds_staging_to_the_claim_on_production_https(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "claims" / CLAIM
    staging.mkdir(parents=True)
    left, right = socket.socketpair()
    try:
        session = BrowserSupervisorSession(
            claim_id=CLAIM,
            container_id="a" * 64,
            profile="container-https",
            origin="https://phase1a.qa.clinic-os.dev:8443",
            runner_pid=123,
            control_socket=left,
            next_sequence=0,
            staging_claim_id=CLAIM,
            staging_root=staging,
            publisher_claim_id="44444444-4444-4444-8444-444444444444",
            publication_authorization_id="f3-artifacts",
        )
        assert session.take_sequence() == 0
        assert session.next_sequence == 1
        with pytest.raises(SupervisorSessionError, match="staging claim"):
            BrowserSupervisorSession(
                claim_id=CLAIM,
                container_id="a" * 64,
                profile="container-https",
                origin="https://phase1a.qa.clinic-os.dev:8443",
                runner_pid=123,
                control_socket=left,
                next_sequence=0,
                staging_claim_id=CLAIM,
                staging_root=tmp_path / "elsewhere",
                publisher_claim_id="44444444-4444-4444-8444-444444444444",
                publication_authorization_id="f3-artifacts",
            )
    finally:
        left.close()
        right.close()


def test_an_unacknowledged_publication_aborts_the_session(tmp_path: Path) -> None:
    with pytest.raises(ArtifactPublicationError, match="never acknowledged"):
        run_recorded_session(tmp_path, frozenset({"no-acknowledgement"}))

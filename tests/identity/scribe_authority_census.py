"""Execute real capture scope, encrypted delivery and context-switch witnesses."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol

from apps.core.patient_context import ContextSwitch
from apps.scribe.contracts import LIVE_STATES
from apps.scribe.models import RecordingSession
from apps.scribe.switches import CaptureSwitchGuard, bound_recordings
from apps.tenancy.db import tenant_context
from django.test import override_settings

from patient_service_support import runtime_role
from scribe import test_ingest, test_refusals

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rbac_fixtures import RbacGraph


class _ProofRecorder(Protocol):
    def run(self, label: str, oracle: Callable[[], None]) -> None: ...


def _context_switch_scope(graph: RbacGraph) -> None:
    scope = test_ingest.world(graph)
    guard = CaptureSwitchGuard()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        recording = RecordingSession.objects.get(pk=scope.session)
        binding = recording.enrollment_id, recording.encounter_id
        switch = ContextSwitch(graph.clinic_a, recording.enrollment_id, None)
        consequence = guard.check(switch)
        assert consequence is not None
        assert consequence.key == "scribe.capture"
        assert tuple(bound_recordings(switch).values_list("pk", flat=True)) == (
            scope.session,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not bound_recordings(switch).exists()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        guard.discard(switch)
        recording.refresh_from_db()
        assert (recording.enrollment_id, recording.encounter_id) == binding
        assert recording.state not in LIVE_STATES
        assert guard.check(switch) is None


def exercise_scribe_authority(
    proofs: _ProofRecorder,
    graph: Callable[[], RbacGraph],
    secret_root: Path,
) -> None:
    """Issue certificates only after real positive and refusal paths execute."""
    with override_settings(
        SCRIBE_SYNTHETIC_CAPTURE=True,
        SCRIBE_MEDIA_ROOT=secret_root.parent / "scribe-census-media",
    ):
        proofs.run(
            "Scribe encrypted delivery and assigned-reader scope",
            partial(
                test_ingest.test_outbox_stores_only_ciphertext_and_object_keys_are_not_authority,
                graph(),
            ),
        )
        proofs.run(
            "Scribe current consent and direct SQL refusal",
            partial(
                test_ingest.test_revoked_consent_stops_new_chunks_and_direct_sql,
                graph(),
            ),
        )
        with test_refusals.capture_api(graph()) as api:
            for field in ("session_id", "epoch"):
                proofs.run(
                    f"Scribe HTTP foreign binding refusal:{field}",
                    partial(
                        test_refusals.test_unknown_and_foreign_record_references_never_write,
                        api,
                        field,
                    ),
                )
            proofs.run(
                "Scribe HTTP epoch fence and valid pause",
                partial(
                    test_refusals.test_epoch_rollback_and_paused_upload_are_read_only,
                    api,
                ),
            )
        proofs.run(
            "Scribe context-switch ownership and immutable encounter binding",
            partial(_context_switch_scope, graph()),
        )

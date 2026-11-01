"""One reusable, consented synthetic visit for drafting and authority probes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.policy import OrganizationPolicy
from apps.core.integration import execute_operation
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import create_draft
from apps.identity.clinic_configuration import latest_configuration
from apps.scribe.draft_contracts import SECTIONS, DraftRequest
from apps.scribe.models import AIDraft, ChunkTranscription
from apps.scribe.services import (
    control_recording,
    finalize_transcript,
    read_transcript,
    request_draft,
    review_segment,
)
from apps.scribe.transcript_contracts import SegmentReview
from apps.tenancy.db import tenant_context

from ai.support import model
from patient_service_support import runtime_role
from scribe.test_ingest import World, world
from scribe.test_transcript_runtime import accepted, admit_interactive_work, permit_asr

if TYPE_CHECKING:
    from uuid import UUID

    import pytest

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class DraftWorld:
    """All probes reuse these subjects rather than reseeding the census."""

    graph: RbacGraph
    recording: World
    version: UUID
    transcript: UUID
    configuration: UUID
    administrator: UUID


def seed_draft_world(graph: RbacGraph, patch: pytest.MonkeyPatch) -> DraftWorld:
    # Imported setup cannot inherit todo 41's module-scoped autouse fixture.
    # Invoke that same fixture; all media, authority and gateway stages stay real.
    admit_interactive_work._get_wrapped_function()(patch)
    scope = world(graph)
    permit_asr(graph, scope, patch)
    configuration = model(
        organization=graph.organization_a,
        clinic=graph.clinic_a,
        purpose=Purpose.SCRIBE_DRAFT,
    )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        current = latest_configuration(graph.clinic_a)
        assert current is not None
        administrator = current.published_by_id
    with runtime_role(), tenant_context(administrator, graph.organization_a):
        current = latest_configuration(graph.clinic_a)
        assert current is not None
        set_organization_policy(
            clinic_id=graph.clinic_a,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("scribe_asr", "scribe_draft"),
                monthly_budget_micros=10_000_000,
                purpose_budgets=(
                    ("scribe_asr", 10_000_000),
                    ("scribe_draft", 10_000_000),
                ),
            ),
        )
    for sequence in range(2):
        media_operation = accepted(graph, scope, sequence=sequence)
        from apps.scribe.tasks import run_transcription  # noqa: PLC0415

        with runtime_role():
            assert run_transcription(operation_id=str(media_operation)) == "queued"
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            inference = ChunkTranscription.objects.get(
                session_id=scope.session, chunk__seq=sequence
            ).invocation.operation_id
        assert inference is not None
        with runtime_role():
            assert execute_operation(inference) == "succeeded"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current_transcript = read_transcript(
            clinic_id=graph.clinic_a, session_id=scope.session
        )
        assert current_transcript is not None
        current_transcript = review_segment(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=current_transcript.revision,
            review=SegmentReview(
                current_transcript.segments[0].segment_id, speaker="patient"
            ),
        )
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        transcript = finalize_transcript(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            expected_revision=current_transcript.revision,
            acknowledge_incomplete=False,
        )
        template = SpecialtyTemplate.objects.filter(clinic_id=graph.clinic_a).first()
        assert template is not None
        assert transcript.version_id is not None
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=scope.encounter,
            template_id=template.pk,
        )
    return DraftWorld(
        graph, scope, version.pk, transcript.version_id, configuration.pk, administrator
    )


def queue(scope: DraftWorld, *, sections: tuple[str, ...] = SECTIONS) -> AIDraft:
    from apps.ehr.models import ClinicalDocumentVersion  # noqa: PLC0415

    graph = scope.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        return request_draft(
            clinic_id=graph.clinic_a,
            version_id=scope.version,
            transcript_id=scope.transcript,
            request_key=uuid4(),
            request=DraftRequest(version.revision, sections),
        )


def generate(scope: DraftWorld) -> AIDraft:
    queued = queue(scope)
    graph = scope.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        operation = AIDraft.objects.get(pk=queued.pk).invocation.operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    return queued

"""Synthetic browser sources and real runtime-role gateway settlement."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.owner import set_global_switch
from apps.ai.policy import OrganizationPolicy
from apps.consent.services import (
    consent_for_future_use,
    prepare_acceptance,
    publish_text,
    record_consent,
)
from apps.core.integration import execute_operation
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import Clinic, User, UserClinicRole
from apps.intake.patient_access import patient_session_context
from apps.providers.lifecycle import VersionInput, propose_version
from apps.scribe.authority import encounter_scope
from apps.scribe.contracts import ChunkInput
from apps.scribe.models import (
    AIDraft,
    AudioChunk,
    CaptureEpoch,
    ChunkTranscription,
    DraftSectionSuggestion,
    ReviewDecision,
)
from apps.scribe.services import (
    control_recording,
    create_recording,
    finalize_transcript,
    ingest_chunk,
    issue_encounter_token,
    read_transcript,
    review_segment,
)
from apps.scribe.transcript_contracts import SegmentReview
from apps.scribe.transcription import prepare_audio, request_transcription
from apps.tenancy.db import tenant_context
from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.db import connection

from ai.support import model
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest, ProviderResult
    from apps.scribe.gateway import ScribeSyntheticProvider


def configure(*, clinic: UUID, organization: UUID) -> UUID:
    """The fixture owner assigns actual organization-wide policy authority."""
    administrator = User.objects.create(username=f"synthetic-review-{uuid4().hex}")
    with owner_context(organization):
        for target in Clinic.objects.filter(organization_id=organization):
            UserClinicRole.objects.create(
                organization_id=organization,
                clinic=target,
                user=administrator,
                role="org_admin",
            )
        UserClinicRole.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            user=administrator,
            role="clinic_admin",
        )
    for capability in ("llm_inference", "asr"):
        propose_version(
            capability,
            version_input=VersionInput(
                provider="synthetic",
                account=f"synthetic-review-{uuid4().hex}",
                environment="synthetic",
                region="synthetic",
                retention_terms="synthetic-no-retention",
            ),
        )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    for purpose in (Purpose.SCRIBE_ASR, Purpose.SCRIBE_DRAFT):
        model(purpose=purpose)
    with runtime_role(), tenant_context(administrator.pk, organization):
        configuration = latest_configuration(clinic)
        if configuration is None:
            configuration = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic review clinic"),
            )
        set_organization_policy(
            clinic_id=clinic,
            expected_version=configuration.version,
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
    return administrator.pk


def recognize(*, chunk_id: UUID, clinic: UUID, actor: UUID, organization: UUID) -> None:
    """Call the actual worker stages; queue fairness is not this fixture's subject."""
    with runtime_role(), tenant_context(actor, organization):
        operation = AudioChunk.objects.get(pk=chunk_id).operation_id
    assert operation is not None
    with runtime_role():
        assert execute_operation(operation) == "succeeded"
    with runtime_role(), tenant_context(actor, organization):
        prepared = prepare_audio(clinic_id=clinic, chunk_id=chunk_id)
    ciphertext = prepared.storage.get(prepared.key)
    with runtime_role(), tenant_context(actor, organization):
        assert (
            request_transcription(
                clinic_id=clinic, chunk_id=chunk_id, ciphertext=ciphertext
            )
            == "queued"
        )
        inference = ChunkTranscription.objects.get(chunk_id=chunk_id).invocation
        inference_operation = inference.operation_id
    assert inference_operation is not None
    with runtime_role():
        assert execute_operation(inference_operation) == "succeeded"


def seed(*, actor: UUID, clinic: UUID, organization: UUID, appointment: UUID) -> UUID:
    """Build a consented final transcript for the browser's existing open note."""
    administrator = configure(clinic=clinic, organization=organization)
    with runtime_role(), tenant_context(actor, organization):
        encounter = Encounter.objects.get(
            clinic_id=clinic, physician_id=actor, appointment_id=appointment
        )
        version = ClinicalDocumentVersion.objects.get(
            document__encounter=encounter, state="draft"
        )
        scope = encounter_scope(clinic_id=clinic, encounter_id=encounter.pk)
        acceptance = consent_for_future_use(
            clinic_id=clinic,
            enrollment_id=scope.enrollment.pk,
            purpose="consultation_recording",
        )
        assert acceptance is not None
        patient_session = acceptance.patient_session_id
    with runtime_role(), tenant_context(administrator, organization):
        text = publish_text(
            clinic_id=clinic,
            purpose="ai_assistance",
            text="SINTETICO-42: consentimento específico para assistência de IA.",
        )
    with runtime_role(), patient_session_context(patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        record_consent(offer=offer, purpose="ai_assistance", accepted=True)
    with runtime_role(), tenant_context(actor, organization):
        token = issue_encounter_token(
            clinic_id=clinic,
            encounter_id=encounter.pk,
            browser_binding="synthetic-review-fixture",
        )
        recording = create_recording(
            clinic_id=clinic, encounter_token=token, command_id=uuid4()
        )
        control_recording(
            clinic_id=clinic,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="start",
            expected_epoch=0,
        )
        epoch = CaptureEpoch.objects.get(session=recording, number=1)
    for sequence in range(2):
        content = f"SINTETICO-ASR-FIXTURE:{sequence}".encode("ascii")
        with runtime_role(), tenant_context(actor, organization):
            assert (
                ingest_chunk(
                    clinic_id=clinic,
                    encounter_token=token,
                    chunk=ChunkInput(
                        session_id=recording.pk,
                        epoch=epoch.pk,
                        track="microphone",
                        seq=sequence,
                        sha256=sha256(content).hexdigest(),
                        duration_ms=2000,
                        codec="audio/webm;codecs=opus",
                        data=content,
                    ),
                ).ack
                == "received"
            )
            chunk_id = AudioChunk.objects.get(
                session=recording, epoch=epoch, seq=sequence
            ).pk
        recognize(
            chunk_id=chunk_id, clinic=clinic, actor=actor, organization=organization
        )
    with runtime_role(), tenant_context(actor, organization):
        transcript = read_transcript(clinic_id=clinic, session_id=recording.pk)
        assert transcript is not None
        reviewed = review_segment(
            clinic_id=clinic,
            session_id=recording.pk,
            expected_revision=transcript.revision,
            review=SegmentReview(transcript.segments[0].segment_id, speaker="patient"),
        )
        control_recording(
            clinic_id=clinic,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        finalize_transcript(
            clinic_id=clinic,
            session_id=recording.pk,
            expected_revision=reviewed.revision,
            acknowledge_incomplete=False,
        )
    return version.pk


def execute(*, actor: UUID, organization: UUID, version: UUID, corrupt: bool) -> None:
    """Even a deliberately foreign citation passes through the real validator."""
    from apps.scribe.gateway import ScribeSyntheticProvider  # noqa: PLC0415

    with runtime_role(), tenant_context(actor, organization):
        draft = (
            AIDraft.objects.filter(version_id=version).order_by("-generation").first()
        )
        assert draft is not None
        operation = draft.invocation.operation_id
    assert operation is not None
    original = ScribeSyntheticProvider.complete

    def foreign(
        self: ScribeSyntheticProvider, *, request: ProviderRequest
    ) -> ProviderResult:
        result = original(self, request=request)
        response = json.loads(result.content)
        response["suggestions"][0]["citations"][0]["id"] = str(uuid4())
        return replace(result, content=json.dumps(response))

    with pytest.MonkeyPatch.context() as patch, runtime_role():
        if corrupt:
            patch.setattr(
                "apps.scribe.gateway.ScribeSyntheticProvider.complete", foreign
            )
        assert execute_operation(operation) == "succeeded"


def main() -> None:
    """Require the owner's isolated synthetic fixture channel; emit metadata only."""
    assert resolved_data_mode() == SYNTHETIC_DATA_MODE
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
    mode, raw_actor, raw_clinic, raw_organization, identifier = sys.argv[1:]
    actor, clinic, organization = map(UUID, (raw_actor, raw_clinic, raw_organization))
    if mode == "seed":
        version = seed(
            actor=actor,
            clinic=clinic,
            organization=organization,
            appointment=UUID(identifier),
        )
    else:
        version = UUID(identifier)
        if mode in {"execute", "foreign"}:
            execute(
                actor=actor,
                organization=organization,
                version=version,
                corrupt=mode == "foreign",
            )
        else:
            assert mode == "inspect"
    with runtime_role(), tenant_context(actor, organization):
        note = ClinicalDocumentVersion.objects.get(pk=version)
        draft = (
            AIDraft.objects.filter(version_id=version).order_by("-generation").first()
        )
        result = {
            "version": str(version),
            "revision": note.revision,
            "state": note.state,
            "decisions": ReviewDecision.objects.filter(version_id=version).count(),
            "suggestions": DraftSectionSuggestion.objects.filter(
                version_id=version
            ).count(),
            "inference": draft.invocation.status if draft else "not_requested",
        }
    sys.stdout.write(json.dumps(result) + "\n")

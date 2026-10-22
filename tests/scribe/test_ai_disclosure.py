"""Recording consent cannot substitute for the encounter's AI-use disclosure."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_ai_disclosure,
    record_consent,
)
from apps.ehr.services import open_encounter
from apps.intake.patient_access import patient_session_context
from apps.providers.services import current_version
from apps.scribe.contracts import Reason, ScribeError
from apps.scribe.models import RecordingSession
from apps.scribe.services import (
    control_recording,
    create_recording,
    issue_encounter_token,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from provider_gate_support import seed_capabilities
from renewal.test_encounters import seed_appointment_setup_for_existing
from renewal.test_teleconsult_sessions import seed
from scheduling.appointment_service_support import arrived_synthetic_appointment
from scribe.test_ingest import media

if TYPE_CHECKING:
    from apps.consent.models import ConsentAcceptance
    from apps.ehr.models import Encounter

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)
DISCLOSURES = (None, (False, False), (True, True))


@dataclass(frozen=True, slots=True)
class CaptureSubject:
    encounter: Encounter
    acceptance: ConsentAcceptance
    token: str = field(repr=False)


def subject(graph: RbacGraph, disclosure: tuple[bool, bool] | None) -> CaptureSubject:
    """Use real recording consent and the public encounter-attestation service."""
    seed_capabilities()
    registration(graph, graph.physician, graph.clinic_a)
    _appointment, encounter, _consent, patient, manager = seed(graph)
    with runtime_role(), tenant_context(manager, graph.organization_a):
        text = publish_text(
            clinic_id=graph.clinic_a,
            purpose="consultation_recording",
            text="Consentimento sintético específico para gravação.",
        )
    with runtime_role(), patient_session_context(patient):
        _text, offer = prepare_acceptance(text_id=text.pk)
        acceptance = record_consent(
            offer=offer, purpose="consultation_recording", accepted=True
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        if disclosure is not None:
            record_ai_disclosure(
                clinic_id=graph.clinic_a,
                encounter_id=encounter.pk,
                informed=disclosure[0],
                refused=disclosure[1],
            )
        token = issue_encounter_token(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            browser_binding="synthetic-disclosure-browser",
        )
    return CaptureSubject(encounter, acceptance, token)


def raw_recording(graph: RbacGraph, scope: CaptureSubject) -> RecordingSession:
    """Exercise the real SQL insert guard, without the Python admission helper."""
    version = current_version("object_storage_media", clinic_id=graph.clinic_a)
    assert version is not None
    digest = sha256(scope.token.encode()).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.scribe_token',%s,true)", [digest])
    return RecordingSession.objects.create(
        organization_id=graph.organization_a,
        clinic_id=graph.clinic_a,
        encounter=scope.encounter,
        enrollment_id=scope.acceptance.enrollment_id,
        physician_id=graph.physician,
        consent=scope.acceptance,
        creation_key=uuid4(),
        token_digest=digest,
        capability_version_id=version.pk,
        capability_snapshot={
            "capability": "object_storage_media",
            "provider": version.provider,
            "account": version.account,
            "environment": version.environment,
            "api_version": version.api_version,
            "region": version.region,
            "retention_terms": version.retention_terms,
        },
    )


@pytest.mark.parametrize("disclosure", DISCLOSURES)
def test_capture_refuses_missing_uninformed_or_refused_disclosure(
    rbac_graph: RbacGraph, disclosure: tuple[bool, bool] | None
) -> None:
    # Given: recording was accepted but this encounter lacks AI-use permission.
    graph = rbac_graph
    scope = subject(graph, disclosure)
    # When: the assigned clinician requests capture through the real service.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ScribeError) as denied,
    ):
        create_recording(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            command_id=uuid4(),
        )
    # Then: use the existing recording-consent denial, not a new patient signal.
    assert denied.value.code == Reason.CONSENT_REVOKED


@pytest.mark.parametrize("disclosure", DISCLOSURES)
def test_sql_capture_refuses_missing_uninformed_or_refused_disclosure(
    rbac_graph: RbacGraph, disclosure: tuple[bool, bool] | None
) -> None:
    # Given: the same patient accepted recording, independently of AI disclosure.
    graph = rbac_graph
    scope = subject(graph, disclosure)
    # When: clinic_app writes directly instead of calling Python admission.
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(DatabaseError) as denied,
        transaction.atomic(),
    ):
        raw_recording(graph, scope)
    # Then: the database independently refuses capture authority.
    assert getattr(denied.value.__cause__, "sqlstate", None) == "42501"


def test_capture_allows_the_current_informed_unrefused_disclosure(
    rbac_graph: RbacGraph,
) -> None:
    # Given: both recording consent and this encounter's disclosure are current.
    graph = rbac_graph
    scope = subject(graph, (True, False))
    # When: the assigned clinician authorizes capture.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        recording = create_recording(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            command_id=uuid4(),
        )
    # Then: recording admission remains available.
    assert recording.state == "authorized"


def test_sql_capture_allows_the_current_informed_unrefused_disclosure(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a valid disclosure and otherwise identical raw-insert terms.
    graph = rbac_graph
    scope = subject(graph, (True, False))
    # When: clinic_app inserts under the real SQL authority.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        recording = raw_recording(graph, scope)
    # Then: the negative SQL cases cannot pass merely from invalid fixture data.
    assert recording.state == "created"


def test_unknown_and_populated_wrong_clinic_recordings_have_identical_denial(
    rbac_graph: RbacGraph,
) -> None:
    # Given: an actual authorized recording in clinic A.
    graph = rbac_graph
    scope = subject(graph, (True, False))
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        recording = create_recording(
            clinic_id=graph.clinic_a,
            encounter_token=scope.token,
            command_id=uuid4(),
        )
        denials = []
        # When: request an unknown record or that real record from clinic B.
        for clinic, identifier in (
            (graph.clinic_a, uuid4()),
            (graph.clinic_b, recording.pk),
        ):
            with pytest.raises(ScribeError) as denied:
                control_recording(
                    clinic_id=clinic,
                    session_id=identifier,
                    encounter_token=scope.token,
                    command_id=uuid4(),
                    action="start",
                    expected_epoch=0,
                )
            denials.append(denied.value.code)
    # Then: the populated foreign-clinic record is not disclosed.
    assert denials == [Reason.ACCESS_DENIED, Reason.ACCESS_DENIED]


@pytest.mark.parametrize("direct_sql", [False, True])
def test_disclosure_from_another_encounter_is_not_current_for_this_capture(
    rbac_graph: RbacGraph, *, direct_sql: bool
) -> None:
    # Given: this patient was informed for an earlier, different encounter.
    graph = rbac_graph
    earlier = subject(graph, (True, False))
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        appointment = earlier.encounter.appointment
        assert appointment is not None
        setup = seed_appointment_setup_for_existing(graph, appointment)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = arrived_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        current = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        token = issue_encounter_token(
            clinic_id=graph.clinic_a,
            encounter_id=current.pk,
            browser_binding="synthetic-current-disclosure",
        )
        # When: recording consent is current, but only the old disclosure exists.
        if direct_sql:
            with pytest.raises(DatabaseError) as refused, transaction.atomic():
                raw_recording(graph, CaptureSubject(current, earlier.acceptance, token))
            assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"
            return
        with pytest.raises(ScribeError) as denied:
            create_recording(
                clinic_id=graph.clinic_a,
                encounter_token=token,
                command_id=uuid4(),
            )
    # Then: disclosure is encounter-specific, not a reusable patient permission.
    assert denied.value.code == Reason.CONSENT_REVOKED

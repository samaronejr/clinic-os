"""Observe document permissions on real scheduled subjects after valid controls."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ehr.models import Encounter
from apps.identity.models import CareTeamMembership, RoleGrant, UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.intake.services import create_patient
from apps.prescription import document_views, documents, services
from apps.prescription.document_forms import DocumentContentForm
from apps.prescription.documents import DocumentContentInput
from apps.prescription.models import DocumentKind, PrescriptionDraft
from apps.scheduling.appointment_lifecycle import arrive
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
    create_availability,
)
from django.contrib.messages.middleware import MessageMiddleware
from django.db import connection, transaction
from django.http import HttpResponse, HttpResponseBase
from django.utils import timezone

from ehr.test_autosave import registration
from identity.delegation_probes import _as_owner
from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.prescription.document_views._selected_draft",
        "apps.prescription.document_views._apply_action",
        "apps.prescription.documents._enrollment",
        "apps.prescription.services.authorize_document_draft",
        "apps.prescription.services.save_document_draft",
        "apps.prescription.services.require_current_document",
    }
)


def document_probes(
    data: NonstaffSubjects, graph: RbacGraph
) -> list[DifferentialProbe]:
    """Allow each actual callable, roll back its command, then subtract permission.

    Preparation gives the census actor real physician, registration, care and
    scheduled-assignment subjects. Observation still runs as clinic_app. Only
    the deciding permission is removed, so signing cannot be refused merely
    because preparation permission or an earlier subject was already missing.
    """
    calls: dict[str, Callable[[], object]] = {}

    def prepare(symbol: str) -> None:
        now = timezone.now()
        with _as_owner(), owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=data.actor,
                role="physician",
            )
            registration(graph, data.actor.pk, graph.clinic_a)
            with runtime_role(), connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('app.current_user_id',true)")
                (previous,) = cursor.fetchone()
                cursor.execute(
                    "SELECT set_config('app.current_user_id',%s,true)",
                    [str(graph.shared_user)],
                )
                try:
                    registered = create_patient(
                        clinic_id=graph.clinic_a,
                        full_name="Sintetico document delegation",
                        birth_date=now.date() - timedelta(days=12000),
                        idempotency_key=uuid4(),
                    )
                    create_availability(
                        clinic_id=graph.clinic_a,
                        practitioner_id=data.actor.pk,
                        start_local="2035-08-01T08:00",
                        end_local="2035-08-01T12:00",
                        idempotency_key=uuid4(),
                    )
                    booked = create_appointment(
                        clinic_id=graph.clinic_a,
                        enrollment_id=registered.enrollment.pk,
                        practitioner_id=data.actor.pk,
                        local_range=AppointmentLocalRange(
                            "2035-08-01T08:00", "2035-08-01T08:30"
                        ),
                        idempotency_key=uuid4(),
                    )
                    booked = arrive(
                        clinic_id=graph.clinic_a,
                        appointment_id=booked.pk,
                        expected_revision=booked.revision,
                        command_id=uuid4(),
                    )
                finally:
                    cursor.execute(
                        "SELECT set_config('app.current_user_id',%s,true)",
                        [previous or ""],
                    )
            CareTeamMembership.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_enrollment=registered.enrollment,
                user=data.actor,
                role="physician",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
            encounter = Encounter.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_id=booked.patient_id,
                physician=data.actor,
                appointment=booked,
            )
            with runtime_role():
                draft = services.create_document_draft(
                    clinic_id=graph.clinic_a,
                    encounter_id=encounter.pk,
                    kind=DocumentKind.CERTIFICATE,
                )
                draft = services.save_document_draft(
                    clinic_id=graph.clinic_a,
                    draft_id=draft.pk,
                    expected_version=1,
                    content=DocumentContentInput(body="Sintetico document control"),
                )
                document = services.render_document(
                    clinic_id=graph.clinic_a,
                    draft_id=draft.pk,
                    expected_version=2,
                    encounter_id=encounter.pk,
                    patient_id=booked.patient_id,
                    issuer_id=data.actor.pk,
                )
            request = data.request(
                {
                    "draft_id": str(draft.pk),
                    "expected_version": "2",
                    "body": "Sintetico delegated save",
                }
            )
            MessageMiddleware(lambda _request: HttpResponse()).process_request(request)
            calls.update(
                {
                    "apps.prescription.document_views._selected_draft": (
                        lambda: document_views._selected_draft(
                            request, graph.clinic_a, ""
                        )
                    ),
                    "apps.prescription.document_views._apply_action": (
                        lambda: document_views._apply_action(
                            request, draft, DocumentContentForm(request.POST), "save"
                        )
                    ),
                    "apps.prescription.documents._enrollment": (
                        lambda: documents._enrollment(encounter)
                    ),
                    "apps.prescription.services.authorize_document_draft": (
                        lambda: services.authorize_document_draft(
                            clinic_id=graph.clinic_a, draft_id=draft.pk
                        )
                    ),
                    "apps.prescription.services.save_document_draft": (
                        lambda: services.save_document_draft(
                            clinic_id=graph.clinic_a,
                            draft_id=draft.pk,
                            expected_version=2,
                            content=DocumentContentInput(
                                body="Sintetico delegated save"
                            ),
                        )
                    ),
                    "apps.prescription.services.require_current_document": (
                        lambda: services.require_current_document(document)
                    ),
                }
            )
            assert set(calls) == SYMBOLS
            with runtime_role(), transaction.atomic():
                result = calls[symbol]()
                if symbol.endswith("._enrollment"):
                    assert isinstance(result, PatientClinicEnrollment)
                    assert result.pk == registered.enrollment.pk
                elif symbol.endswith(".require_current_document"):
                    assert result is None
                elif symbol.endswith("._apply_action"):
                    assert isinstance(result, HttpResponseBase)
                    assert result.status_code == 302
                    assert PrescriptionDraft.objects.get(pk=draft.pk).version == 3
                else:
                    assert isinstance(result, PrescriptionDraft)
                    assert result.pk == draft.pk
                    assert result.version == (
                        3 if symbol.endswith(".save_document_draft") else 2
                    )
                transaction.set_rollback(True)
            RoleGrant.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                role="physician",
                permission=(
                    "prescription.sign"
                    if symbol.endswith(".require_current_document")
                    else "prescription.prepare"
                ),
                valid_from=now - timedelta(days=1),
            )

    probes = []
    for symbol in sorted(SYMBOLS):

        def call(symbol: str = symbol) -> object:
            return calls[symbol]()

        probes.append(
            DifferentialProbe(
                symbol, call, expected=False, prepare=partial(prepare, symbol)
            )
        )
    return probes

"""Synthetic representation fixtures with real RLS and quarantined evidence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ehr.attachments import AttachmentInput, scan_attachment, upload_attachment
from apps.identity.delegates import (
    DelegateGrantRequest,
    activate,
    request_delegate_grant,
    verify_delegate_grant,
)
from apps.identity.models import PatientDelegateGrant
from apps.intake.models import PatientClinicEnrollment
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone

from identity.permission_support import permission_actor, permission_context
from patient_service_support import runtime_role
from renewal.test_amendments import saved_draft
from renewal.test_attachments import PDF_BYTES
from renewal.test_encounters import seed, setup_context
from renewal.test_patient_access import _issue, _redeem, _seed_patients

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path
    from uuid import UUID

    from apps.identity.delegates import DelegateReceipt

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class NotesFixture:
    """A synthetic draft for one represented enrollment."""

    enrollment_id: UUID
    patient_id: UUID
    encounter_id: UUID
    first_version_id: UUID


@dataclass(frozen=True)
class DelegateWorld:
    """No legal authority follows from the delegate's own invitation."""

    notes: NotesFixture
    delegate_enrollment_id: UUID
    session_id: UUID
    verifier_id: UUID
    evidence_id: UUID
    deadline: datetime


def delegate_world(
    graph: RbacGraph, root: Path, *, notes: NotesFixture | None = None
) -> DelegateWorld:
    """Keep a clinical patient, a separate adult identity and scanned evidence."""
    if notes is None:
        appointment, template = seed(graph)
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            first = saved_draft(graph, appointment, template)
            encounter_id = first.document.encounter_id
        with setup_context(graph.organization_a):
            enrollment = PatientClinicEnrollment.objects.get(
                clinic_id=graph.clinic_a, patient_id=appointment.patient_id
            )
        notes = NotesFixture(
            enrollment.pk,
            appointment.patient_id,
            encounter_id,
            first.pk,
        )
    parent = _seed_patients(graph)
    _, secret = _issue(graph, parent.enrollment_id)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    verifier, _ = permission_actor(graph, "clinic_manager")
    with (
        override_settings(EHR_ATTACHMENT_ROOT=root),
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        attachment = upload_attachment(
            clinic_id=graph.clinic_a,
            encounter_id=notes.encounter_id,
            upload=AttachmentInput(
                file_name="synthetic-authority.pdf",
                declared_type="application/pdf",
                data=PDF_BYTES,
            ),
        )
        attachment = scan_attachment(
            clinic_id=graph.clinic_a, attachment_id=attachment.pk
        )
        assert attachment.state == "available"
    return DelegateWorld(
        notes,
        parent.enrollment_id,
        session,
        verifier,
        attachment.pk,
        timezone.now() + timedelta(days=30),
    )


def request_grant(
    graph: RbacGraph,
    world: DelegateWorld,
    *,
    key: UUID | None = None,
    operations: tuple[str, ...] = ("enrollment_view", "booking", "records", "consent"),
    purposes: tuple[str, ...] = ("care_processing", "teleconsultation"),
) -> DelegateReceipt:
    with permission_context(graph, world.verifier_id):
        return request_delegate_grant(
            clinic_id=graph.clinic_a,
            grant=DelegateGrantRequest(
                represented_enrollment_id=world.notes.enrollment_id,
                delegate_enrollment_id=world.delegate_enrollment_id,
                authority_kind=PatientDelegateGrant.AuthorityKind.GUARDIAN,
                evidence_attachment_id=world.evidence_id,
                purposes=purposes,
                operations=operations,
                expires_at=world.deadline,
                idempotency_key=key or uuid4(),
            ),
        )


def active_grant(
    graph: RbacGraph,
    world: DelegateWorld,
    *,
    operations: tuple[str, ...] = ("enrollment_view", "booking", "records", "consent"),
    purposes: tuple[str, ...] = ("care_processing", "teleconsultation"),
) -> DelegateReceipt:
    grant = request_grant(graph, world, operations=operations, purposes=purposes)
    with permission_context(graph, world.verifier_id):
        grant = verify_delegate_grant(
            clinic_id=graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
            verified=True,
        )
        return activate(
            clinic_id=graph.clinic_a,
            grant_id=grant.grant_id,
            expected_revision=grant.revision,
        )


@contextmanager
def delegate_clock(at: datetime) -> Iterator[None]:
    """Replace the owner-only clock, not authorization, and restore it exactly."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "CREATE OR REPLACE FUNCTION clinic_app.delegate_now() "
            "RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER "
            "SET search_path=pg_catalog,clinic_app,pg_temp AS "
            "$f$ SELECT %s::timestamptz $f$".replace("%s", "'" + at.isoformat() + "'")
        )
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                "CREATE OR REPLACE FUNCTION clinic_app.delegate_now() "
                "RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER "
                "SET search_path=pg_catalog,clinic_app,pg_temp AS "
                "$f$ SELECT statement_timestamp() $f$"
            )
            cursor.execute("RESET ROLE")

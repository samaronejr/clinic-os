"""Real reviewed subjects for the fiscal preparation permission boundary."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.billing.errors import BillingAccessDeniedError
from apps.billing.fiscal import prepare_fiscal_document
from apps.billing.journal import create_book
from apps.billing.models import FiscalConfiguration, FiscalDocument
from apps.billing.services import create_invoice, issue_invoice
from apps.identity.models import UserClinicRole
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from identity.delegation_probe_support import Case, Seeded
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


def _prepare_document(graph: RbacGraph, _actor: UUID) -> Seeded:
    with owner_context(graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.shared_user,
            role=UserClinicRole.Role.OWNER,
        )
        UserClinicRole.objects.get_or_create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.FINANCE,
        )
        patient = Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Synthetic fiscal delegation",
            birth_date=date(1990, 1, 1),
        )
        PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"f" * 32,
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.current_user_id',true)")
        (previous_actor,) = cursor.fetchone()
    try:
        with (
            permission_context(graph, graph.shared_user),
            override_settings(BILLING_SYNTHETIC_PIX=True),
        ):
            book = create_book(clinic_id=graph.clinic_a)
            invoice = create_invoice(
                clinic_id=graph.clinic_a,
                patient_id=patient.pk,
                amount_minor=1000,
                idempotency_key=uuid4(),
            )
            issue_invoice(
                clinic_id=graph.clinic_a,
                invoice_id=invoice.pk,
                expected_revision=invoice.revision,
            )
        with owner_context(graph.organization_a):
            configuration = FiscalConfiguration.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                book=book,
                legal_entity_reference=uuid4(),
                legal_entity={"name": "Synthetic clinic", "registration": "SYNTHETIC"},
                provider="national",
                municipality_code="0000000",
                service_code="SYNTHETIC-SERVICE",
                tax_regime="SYNTHETIC",
                ibs_cbs_applicability="not_applicable",
                tax_fields={"ISS": {"classification": "SYNTHETIC"}},
                review_reference=uuid4(),
                review_kind="synthetic_fixture",
                reviewed_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=1),
                cancellation_window_seconds=3600,
            )
        with permission_context(graph, graph.clinic_admin):
            control = prepare_fiscal_document(
                clinic_id=graph.clinic_a,
                invoice_id=invoice.pk,
                configuration_id=configuration.pk,
                command_id=uuid4(),
            )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [previous_actor or ""],
            )
    command = uuid4()

    def state() -> object:
        with permission_context(graph, graph.clinic_admin):
            assert FiscalDocument.objects.filter(pk=control.pk).exists()
            return tuple(
                FiscalDocument.objects.filter(create_command_id=command).values_list(
                    "pk", flat=True
                )
            )

    return Seeded(
        lambda: prepare_fiscal_document(
            clinic_id=graph.clinic_a,
            invoice_id=invoice.pk,
            configuration_id=configuration.pk,
            command_id=command,
        ),
        state,
        denial=BillingAccessDeniedError,
    )


CASES = {
    "apps.billing.fiscal.prepare_fiscal_document": Case(
        _prepare_document,
        database_role="clinic_app",
        permission="settlement.post",
        permission_role="finance",
    ),
}

"""Direct SQL authority oracles; role expectations never come from SQL bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.billing import pix
from apps.billing import services as billing
from apps.consent.services import publish_text
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import _kek
from django.db import connection
from psycopg import sql

from identity.legacy_operational_boundaries import seed_operational
from identity.legacy_owner_boundaries import _owner_call
from identity.legacy_parity_support import ADMINS, LEGACY, MANAGERS, PHYSICIAN, world
from identity.legacy_teleconsult_boundaries import seed_teleconsult
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    import pytest

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld
    from identity.legacy_teleconsult_boundaries import TeleconsultSubjects
    from rbac_fixtures import RbacGraph

ALL_ROLES = tuple(UserClinicRole.Role.values)
type SqlArgument = str | UUID | list[str] | int | None


@dataclass(frozen=True)
class SqlWorld:
    actor: LegacyWorld
    operational: OperationalSubjects
    teleconsult: TeleconsultSubjects
    manager_payment: UUID
    actor_payment: UUID


def seed_sql_world(
    graph: RbacGraph, role: str, monkeypatch: pytest.MonkeyPatch
) -> SqlWorld:
    actor = world(graph, role)
    op = seed_operational(actor)
    tc = seed_teleconsult(actor, op, monkeypatch)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        manager_payment = pix.prepare_pix_charge(
            clinic_id=actor.clinic, invoice_id=op.invoice.pk
        )
        invoice = billing.create_invoice(
            clinic_id=actor.clinic,
            patient_id=actor.appointment.patient_id,
            amount_minor=100,
            idempotency_key=uuid4(),
        )
        invoice = billing.issue_invoice(
            clinic_id=actor.clinic, invoice_id=invoice.pk, expected_revision=1
        )
    # A legitimately prepared operation retains its stored actor after that
    # actor loses the old manager role. This is a distinct recorder allow path.
    with owner_context(graph.organization_a):
        membership, added = UserClinicRole.objects.get_or_create(
            user_id=actor.actor.pk,
            clinic_id=actor.clinic,
            organization_id=graph.organization_a,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with runtime_role(), tenant_context(actor.actor.pk, graph.organization_a):
        actor_payment = pix.prepare_pix_charge(
            clinic_id=actor.clinic, invoice_id=invoice.pk
        )
    if added:
        with owner_context(graph.organization_a):
            membership.delete()
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        # Give teleconsult_fail a real, stored failure condition, not just a
        # caller-supplied reason; each invocation is subsequently rolled back.
        publish_text(
            clinic_id=actor.clinic,
            purpose="teleconsultation",
            text="Sintetico superseded",
        )
    return SqlWorld(actor, op, tc, manager_payment.pk, actor_payment.pk)


@dataclass(frozen=True)
class SqlProbe:
    name: str
    roles: tuple[str, ...]
    arguments: Callable[[SqlWorld, bool], list[SqlArgument]]
    result: str = "rows"
    refusal_state: str | None = None


def _bound_actor(w: SqlWorld, valid: bool) -> list[SqlArgument]:
    if not valid:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(uuid4())]
            )
    return []


def _booking(w: SqlWorld, valid: bool, *, slots: bool) -> list[SqlArgument]:
    # These patient-facing resolvers still inspect the clinician's staff
    # membership. They are staff-dependent, not a nonstaff exemption. Keep
    # patient credentials valid in both cases; only staff membership changes.
    if not valid:
        _owner_call(
            lambda: UserClinicRole.objects.filter(
                user_id=w.actor.graph.physician,
                clinic_id=w.actor.clinic,
                role=UserClinicRole.Role.PHYSICIAN,
            ).delete()
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', '', true), "
            "set_config('app.current_tenant', '', true), "
            "set_config('app.current_patient_session', %s, true)",
            [str(w.operational.patient_session)],
        )
    return ["2035-06-03", None] if slots else [w.actor.graph.physician]


def call(probe: SqlProbe, w: SqlWorld, valid: bool) -> bool:
    arguments = probe.arguments(w, valid)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT * FROM clinic_app.{}({})").format(
                sql.Identifier(probe.name),
                sql.SQL(",").join(sql.Placeholder() for _ in arguments),
            ),
            arguments,
        )
        rows = cursor.fetchall()
    if probe.result == "boolean":
        return bool(rows == [(True,)])
    if probe.result == "count":
        return bool(len(rows) == 1 and rows[0][0] > 0)
    return bool(rows and rows[0] != (None,))


PROBES = {
    "patient_booking_practitioner": SqlProbe(
        "patient_booking_practitioner",
        ALL_ROLES,
        lambda w, ok: _booking(w, ok, slots=False),
        "boolean",
    ),
    "patient_booking_slots": SqlProbe(
        "patient_booking_slots",
        ALL_ROLES,
        lambda w, ok: _booking(w, ok, slots=True),
    ),
    "auth_lookup": SqlProbe(
        "auth_lookup",
        ALL_ROLES,
        lambda w, ok: [w.actor.actor.username if ok else "synthetic-missing-sql-actor"],
    ),
    "load_current_user": SqlProbe("load_current_user", ALL_ROLES, _bound_actor),
    "user_organizations": SqlProbe("user_organizations", ALL_ROLES, _bound_actor),
    "user_has_org": SqlProbe(
        "user_has_org",
        ALL_ROLES,
        lambda w, ok: [
            w.actor.graph.organization_a if ok else w.actor.graph.organization_b
        ],
        "boolean",
    ),
    "list_active_clinic_physicians": SqlProbe(
        "list_active_clinic_physicians",
        MANAGERS,
        lambda w, ok: [w.actor.clinic_for(ok)],
    ),
    "questionnaire_staff": SqlProbe(
        "questionnaire_staff",
        LEGACY,
        lambda w, ok: [w.actor.clinic_for(ok), list(LEGACY)],
        "boolean",
    ),
    "questionnaire_staff#explicit_new_role": SqlProbe(
        "questionnaire_staff",
        ALL_ROLES,
        lambda w, ok: [w.actor.clinic_for(ok), [w.actor.role]],
        "boolean",
    ),
    "questionnaire_completion": SqlProbe(
        "questionnaire_completion",
        LEGACY,
        lambda w, ok: [w.actor.clinic_for(ok), w.operational.enrollment],
    ),
    "ehr_assigned": SqlProbe(
        "ehr_assigned",
        PHYSICIAN,
        lambda w, ok: [w.actor.encounter if ok else uuid4()],
        "boolean",
    ),
    "ehr_care": SqlProbe(
        "ehr_care",
        PHYSICIAN,
        lambda w, ok: [w.actor.encounter if ok else uuid4()],
        "boolean",
    ),
    "ehr_history_care": SqlProbe(
        "ehr_history_care",
        PHYSICIAN,
        lambda w, ok: [w.actor.encounter if ok else uuid4()],
        "boolean",
    ),
    "ehr_version_scope": SqlProbe(
        "ehr_version_scope",
        LEGACY,
        lambda w, ok: [w.actor.clinic_for(ok), w.actor.version.pk],
    ),
    "billing_staff_invoice": SqlProbe(
        "billing_staff_invoice",
        MANAGERS,
        lambda w, ok: [w.operational.invoice.pk if ok else uuid4()],
        "boolean",
    ),
    "billing_payment_event_recorder": SqlProbe(
        "billing_payment_event_recorder",
        MANAGERS,
        lambda w, ok: [w.manager_payment if ok else uuid4()],
        "boolean",
    ),
    "billing_payment_event_recorder#stored_actor": SqlProbe(
        "billing_payment_event_recorder",
        ALL_ROLES,
        lambda w, ok: [w.actor_payment if ok else uuid4()],
        "boolean",
    ),
    "patient_registry_count": SqlProbe(
        "patient_registry_count",
        MANAGERS,
        lambda w, ok: [_kek(), w.actor.clinic_for(ok), "", None],
        "count",
        "42501",
    ),
    "patient_registry_page": SqlProbe(
        "patient_registry_page",
        MANAGERS,
        lambda w, ok: [_kek(), w.actor.clinic_for(ok), "", None, 0, 25],
        "rows",
        "42501",
    ),
    "retention_author_label": SqlProbe(
        "retention_author_label",
        LEGACY,
        lambda w, ok: [w.actor.graph.physician if ok else uuid4()],
    ),
    "retention_care": SqlProbe(
        "retention_care",
        PHYSICIAN,
        lambda w, ok: [w.actor.clinic_for(ok), w.actor.appointment.patient_id],
        "boolean",
    ),
    "retention_care_patients": SqlProbe(
        "retention_care_patients", PHYSICIAN, lambda w, ok: [w.actor.clinic_for(ok)]
    ),
    "retention_record_scope": SqlProbe(
        "retention_record_scope",
        ADMINS,
        lambda w, ok: ["ehr.encounter", w.actor.encounter if ok else uuid4()],
    ),
    "retention_record_scope#version": SqlProbe(
        "retention_record_scope",
        ADMINS,
        lambda w, ok: ["ehr.document_version", w.actor.version.pk if ok else uuid4()],
    ),
    "teleconsult_assigned": SqlProbe(
        "teleconsult_assigned",
        PHYSICIAN,
        lambda w, ok: [w.teleconsult.session.pk if ok else uuid4()],
        "boolean",
    ),
    "teleconsult_fail": SqlProbe(
        "teleconsult_fail",
        PHYSICIAN,
        lambda w, ok: [w.teleconsult.session.pk if ok else uuid4(), "consent_revoked"],
        "boolean",
    ),
    "teleconsult_session_scope": SqlProbe(
        "teleconsult_session_scope",
        ALL_ROLES,
        lambda w, ok: [w.teleconsult.session.pk if ok else uuid4()],
    ),
    "teleconsult_room_state": SqlProbe(
        "teleconsult_room_state",
        ALL_ROLES,
        lambda w, ok: [w.teleconsult.session.pk if ok else uuid4()],
    ),
    "waitlist_staff": SqlProbe(
        "waitlist_staff", MANAGERS, lambda w, ok: [w.actor.clinic_for(ok)], "boolean"
    ),
    "has_permission": SqlProbe(
        "has_permission",
        ALL_ROLES,
        lambda w, ok: [
            "appointment.read_own"
            if w.actor.role == "physician"
            else "appointment.read",
            w.actor.clinic_for(ok),
            None,
        ],
        "boolean",
    ),
}

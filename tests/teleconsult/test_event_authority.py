"""Direct v2 event writes must bind the claimed role to the real principal."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import RoleGrant, User
from apps.teleconsult.models import TeleconsultEvent
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from consent.test_authority import as_owner, member
from consent.test_purposes import _patient_session
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_participants import World, seed_world

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
KINDS = ("reconnected", "audio_only", "video_restored", "removed")
REFUSAL = "teleconsult participant authority required"


@contextmanager
def principal(
    w: World,
    database_role: str,
    *,
    actor: UUID | None = None,
    patient_session: UUID | None = None,
    tenant: UUID | None = None,
) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        assert database_role in {"clinic_app", "clinic_owner"}
        cursor.execute(f"SET LOCAL ROLE {database_role}")
        for name, value in (
            ("app.current_tenant", tenant or w.organization),
            ("app.current_user_id", actor),
            ("app.current_patient_session", patient_session),
        ):
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)",
                [name, "" if value is None else str(value)],
            )
        yield
        transaction.set_rollback(True)


def insert(w: World, kind: str, role: str) -> None:
    TeleconsultEvent.objects.create(
        organization_id=w.organization,
        session_id=w.session,
        kind=kind,
        actor_role=role,
    )


def refused(w: World, kind: str, role: str) -> None:
    with pytest.raises(DatabaseError) as caught, transaction.atomic():
        insert(w, kind, role)
    cause = caught.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.sqlstate == "42501"
    # The trigger must decide before RLS, even when the policy also refuses.
    assert cause.diag.message_primary == REFUSAL


@pytest.mark.parametrize("database_role", ["clinic_app", "clinic_owner"])
def test_v2_events_bind_the_claimed_role_to_each_participant(
    rbac_graph: RbacGraph, synthetic_provider: object, database_role: str
) -> None:
    w = seed_world(rbac_graph)
    with principal(w, database_role, actor=w.graph.physician):
        before = TeleconsultEvent.objects.count()
        for kind in KINDS:
            insert(w, kind, "physician")
        assert TeleconsultEvent.objects.count() == before + len(KINDS)
        for kind in KINDS[:-1]:
            refused(w, kind, "patient")
        assert TeleconsultEvent.objects.count() == before + len(KINDS)
    with principal(w, database_role, patient_session=w.patient_session):
        before = TeleconsultEvent.objects.count()
        for kind in KINDS[:-1]:
            insert(w, kind, "patient")
        assert TeleconsultEvent.objects.count() == before + len(KINDS) - 1
        for kind in KINDS:
            refused(w, kind, "physician")
        assert TeleconsultEvent.objects.count() == before + len(KINDS) - 1


@pytest.mark.parametrize("database_role", ["clinic_app", "clinic_owner"])
def test_v2_clinician_events_recheck_permission_relation_scope_and_activity(
    rbac_graph: RbacGraph, synthetic_provider: object, database_role: str
) -> None:
    w = seed_world(rbac_graph)
    colleague = member(w.consent_world(), "physician", care_team=w.enrollment)
    for actor, tenant in (
        (colleague, w.organization),
        (w.graph.physician, w.graph.organization_b),
        (w.graph.physician, uuid4()),
        (None, w.organization),
    ):
        with principal(w, database_role, actor=actor, tenant=tenant):
            for kind in KINDS:
                refused(w, kind, "physician")
    # Every altered cell is rolled back, so later cells have unchanged authority.
    with principal(w, database_role, actor=w.graph.physician):
        with as_owner():
            User.objects.filter(pk=w.graph.physician).update(is_active=False)
        for kind in KINDS:
            refused(w, kind, "physician")
    with principal(w, database_role, actor=w.graph.physician):
        with as_owner():
            RoleGrant.objects.create(
                organization_id=w.organization,
                clinic_id=w.clinic,
                role="physician",
                permission="clinical.write",
                valid_from=timezone.now() - timedelta(days=1),
            )
        for kind in KINDS:
            refused(w, kind, "physician")
    with principal(w, database_role, actor=w.graph.physician):
        with as_owner(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_professionalregistration "
                "SET revoked_at=statement_timestamp() WHERE user_id=%s",
                [str(w.graph.physician)],
            )
        for kind in KINDS:
            refused(w, kind, "physician")
    # Positive control proves the fixture and each rolled-back mutation recover.
    with principal(w, database_role, actor=w.graph.physician):
        insert(w, "audio_only", "physician")


@pytest.mark.parametrize("database_role", ["clinic_app", "clinic_owner"])
def test_v2_patient_events_refuse_unknown_revoked_and_foreign_sessions(
    rbac_graph: RbacGraph, synthetic_provider: object, database_role: str
) -> None:
    w = seed_world(rbac_graph)
    foreign_session, _ = _patient_session(rbac_graph)
    for patient_session in (None, uuid4(), foreign_session):
        with principal(w, database_role, patient_session=patient_session):
            for kind in KINDS[:-1]:
                refused(w, kind, "patient")
    with principal(w, database_role, patient_session=w.patient_session):
        with as_owner(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.intake_patientsession "
                "SET revoked_at=statement_timestamp() WHERE id=%s",
                [str(w.patient_session)],
            )
        for kind in KINDS[:-1]:
            refused(w, kind, "patient")

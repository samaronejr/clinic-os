"""Execute longitudinal SQL authority and fixed-worker protocols as clinic_app."""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

import pytest
from apps.ehr.longitudinal import record_observation
from apps.ehr.longitudinal_access import patient_scope
from apps.ehr.models import ClinicalDocumentVersion, Observation, TimelineRefresh
from apps.ehr.note_access import grant_note_access, restrict_note, revoke_note_access
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.models import User
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ehr.longitudinal_support import care_reader, chart_world, finalized
from ehr.test_addenda import clinician
from ehr.test_autosave import ROLES, as_actor, permitted
from ehr.test_longitudinal import TEMPERATURE
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

EHR_ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.ehr_pack_guard()": (
        "ehr.test_specialty_actor.test_pack_sql_rechecks_inactive_actor",
        "ehr.test_specialty_actor.test_pack_guard_refuses_forged_installer",
    ),
    "clinic_app.ehr_longitudinal_guard()": (
        "ehr.test_sql_protocols.test_longitudinal_sql_uses_current_role_and_actor",
    ),
    "clinic_app.ehr_note_visible(uuid)": (
        "ehr.test_sql_protocols.test_note_sql_rechecks_grants_scope_and_active_actor",
    ),
    "clinic_app.ehr_timeline_visible(text,uuid,uuid,uuid)": (
        "ehr.test_sql_protocols.test_note_sql_rechecks_grants_scope_and_active_actor",
    ),
    "clinic_app.ehr_timeline_due()": (
        "ehr.test_sql_protocols.test_projection_worker_refuses_every_ambient_authority",
    ),
    "clinic_app.ehr_timeline_refresh(uuid)": (
        "ehr.test_sql_protocols.test_projection_worker_refuses_every_ambient_authority",
    ),
}


@pytest.mark.parametrize("role", ROLES)
def test_longitudinal_sql_uses_current_role_and_actor(
    rbac_graph: RbacGraph, role: str
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    actor = clinician(graph, role, world.enrollment)
    with as_actor(graph, graph.physician):
        observation = record_observation(
            clinic_id=graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
    for active in (True, False, True):
        User.objects.filter(pk=actor).update(is_active=active)
        with permission_context(graph, actor), connection.cursor() as cursor:
            for permission in ("clinical.read", "clinical.write", "observation.write"):
                cursor.execute(
                    "SELECT clinic_app.ehr_patient_permission(%s,%s,%s)",
                    [permission, graph.clinic_a, world.patient],
                )
                assert cursor.fetchone() == (active and role in permitted(permission),)
            actions: tuple[Literal["read", "write"], ...] = ("read", "write")
            for action in actions:
                permission = "clinical.read" if action == "read" else "clinical.write"
                allowed = active and (
                    role in permitted(permission)
                    or (action == "read" and role in permitted("observation.write"))
                )
                if allowed:
                    assert (
                        patient_scope(
                            clinic_id=graph.clinic_a,
                            enrollment_id=world.enrollment,
                            action=action,
                        ).user_id
                        == actor
                    )
                else:
                    with pytest.raises(ClinicalAccessDeniedError):
                        patient_scope(
                            clinic_id=graph.clinic_a,
                            enrollment_id=world.enrollment,
                            action=action,
                        )
            with transaction.atomic():
                changed = Observation.objects.filter(pk=observation.pk).update(
                    provenance="clinician_confirmed",
                    revision=2,
                    confirmed_at=timezone.now(),
                    confirmed_by_id=actor,
                )
                assert changed == int(active and role in permitted("clinical.write"))
                transaction.set_rollback(True)
            if active and role in permitted("clinical.write"):
                with (
                    pytest.raises(DatabaseError, match="invalid provenance transition"),
                    transaction.atomic(),
                ):
                    Observation.objects.filter(pk=observation.pk).update(
                        provenance="clinician_confirmed",
                        revision=2,
                        confirmed_at=timezone.now(),
                        confirmed_by_id=graph.physician,
                    )
    with owner_context(graph.organization_a):
        observation.refresh_from_db()
        assert (observation.revision, observation.confirmed_at) == (1, None)


@pytest.mark.parametrize("role", ROLES)
def test_note_sql_rechecks_grants_scope_and_active_actor(
    rbac_graph: RbacGraph, role: str
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    source = finalized(graph, world.version)
    actor = (
        care_reader(graph, world)
        if role == "physician"
        else clinician(graph, role, world.enrollment)
    )
    grant_id: UUID | None = None
    with permission_context(graph, actor), connection.cursor() as cursor:
        # This predicate adds a restriction; it is not a stand-alone read grant.
        cursor.execute("SELECT clinic_app.ehr_note_visible(%s)", [source.document_id])
        assert cursor.fetchone() == (True,)
        assert ClinicalDocumentVersion.objects.filter(pk=source.pk).exists() == (
            role in permitted("clinical.read")
        )
        cursor.execute(
            "SELECT clinic_app.ehr_note_grantee(%s,%s)", [graph.clinic_a, actor]
        )
        assert cursor.fetchone() == (role == "physician",)
        cursor.execute("SELECT clinic_app.ehr_note_author(%s)", [source.document_id])
        assert cursor.fetchone() == (False,)
    with as_actor(graph, graph.physician), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_note_author(%s)", [source.document_id])
        assert cursor.fetchone() == (True,)
        restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
        if role == "physician":
            grant_id = grant_note_access(
                clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=actor
            ).pk
        else:
            with pytest.raises(ClinicalAccessDeniedError):
                grant_note_access(
                    clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=actor
                )
    for phase, active in enumerate((True, False, True)):
        with owner_context(graph.organization_a), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)",
                [str(graph.clinic_admin)],
            )
            User.objects.filter(pk=actor).update(is_active=active)
        with permission_context(graph, actor), connection.cursor() as cursor:
            # Reactivation cannot resurrect a terminally revoked grant.
            expected = phase == 0 and role == "physician"
            cursor.execute(
                "SELECT clinic_app.ehr_note_visible(%s)", [source.document_id]
            )
            assert cursor.fetchone() == (expected,)
            cursor.execute(
                "SELECT clinic_app.ehr_timeline_visible('note',%s,%s,%s)",
                [source.pk, graph.clinic_a, world.patient],
            )
            assert cursor.fetchone() == (expected,)
            assert (
                ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()
                == expected
            )
            for clinic, patient in (
                (graph.clinic_b, world.patient),
                (graph.clinic_a, uuid4()),
            ):
                cursor.execute(
                    "SELECT clinic_app.ehr_timeline_visible('note',%s,%s,%s)",
                    [source.pk, clinic, patient],
                )
                assert cursor.fetchone() == (False,)
    if role == "physician":
        assert grant_id is not None
        with as_actor(graph, graph.physician):
            revoke_note_access(clinic_id=graph.clinic_a, grant_id=grant_id)
        with permission_context(graph, actor), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.ehr_note_visible(%s)", [source.document_id]
            )
            assert cursor.fetchone() == (False,)


def test_projection_worker_refuses_every_ambient_authority(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    with owner_context(graph.organization_a):
        refresh = TimelineRefresh.objects.get(
            clinic_id=graph.clinic_a, patient_id=world.patient
        )
        before = (refresh.revision, refresh.refreshed_revision, refresh.refreshed_at)
    names = (
        "app.current_user_id",
        "app.current_tenant",
        "app.current_patient_session",
        "app.current_principal",
    )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for bits in product((False, True), repeat=len(names)):
            if not any(bits):
                continue
            for value in (str(uuid4()), "malformed-authority"):
                for name, enabled in zip(names, bits, strict=True):
                    cursor.execute(
                        "SELECT set_config(%s,%s,true)",
                        [name, value if enabled else ""],
                    )
                cursor.execute("SELECT * FROM clinic_app.ehr_timeline_due()")
                assert cursor.fetchall() == []
                cursor.execute(
                    "SELECT clinic_app.ehr_timeline_refresh(%s)", [refresh.pk]
                )
                assert cursor.fetchone() == (False,)
    with owner_context(graph.organization_a):
        refresh.refresh_from_db()
        assert (
            refresh.revision,
            refresh.refreshed_revision,
            refresh.refreshed_at,
        ) == before
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.ehr_timeline_due()")
        assert (refresh.pk,) in cursor.fetchall()
        cursor.execute("SELECT clinic_app.ehr_timeline_refresh(%s)", [refresh.pk])
        assert cursor.fetchone() == (True,)
        cursor.execute("SELECT clinic_app.ehr_timeline_refresh(%s)", [refresh.pk])
        assert cursor.fetchone() == (False,)


def test_longitudinal_functions_keep_exact_execute_privileges() -> None:
    callable_functions = {
        "ehr_patient_permission",
        "ehr_note_author",
        "ehr_note_grantee",
        "ehr_note_visible",
        "ehr_timeline_visible",
        "ehr_timeline_due",
        "ehr_timeline_refresh",
    }
    trigger_functions = {
        "ehr_longitudinal_guard",
        "ehr_note_guard",
        "ehr_copy_finalization_guard",
        "ehr_timeline_enqueue",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname,r.rolname,p.prosecdef,p.proconfig,"
            "has_function_privilege('clinic_app',p.oid,'EXECUTE'),"
            "has_function_privilege('clinic_agent',p.oid,'EXECUTE'),"
            "has_function_privilege('clinic_owner',p.oid,'EXECUTE') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_roles r ON r.oid=p.proowner WHERE n.nspname='clinic_app' "
            "AND p.proname=ANY(%s)",
            [sorted(callable_functions | trigger_functions)],
        )
        rows = cursor.fetchall()
    assert {row[0] for row in rows} == callable_functions | trigger_functions
    for name, owner, definer, config, app, agent, migration in rows:
        assert (owner, definer, config) == (
            "clinic_resolver",
            True,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
        )
        assert (app, agent, migration) == (name in callable_functions, False, False)

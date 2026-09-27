"""Every SQL authority clause on ``is_active`` refuses an inactive actor itself.

Python (``current_context``) and tenant entry (``user_has_org`` through
``tenant_context``) also refuse inactive actors, so a probe that goes through
either never reaches the SQL clause. These probes bind the actor and tenant
GUCs directly as ``clinic_app`` and flip only ``identity_user.is_active``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from django.db import ProgrammingError, connection, transaction
from psycopg.errors import InsufficientPrivilege

from identity.authority_sql import references
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

ACTOR_SETTING = "app.current_user_id"
# The lexer reports PostgreSQL's DO keyword as dynamic SQL; in these bodies it
# is ON CONFLICT ... DO. A DO block cannot run inside a function body without
# EXECUTE, and EXECUTE (like a computed setting name) still fails closed here.
PARSEABLE_OPAQUE = frozenset({"do"})
# Actor readers whose own body has no is_active read. Their inactive refusal,
# if any, comes from a callee; see QA-SUMMARY for fix-a9. A new actor reader
# must either carry its own clause (and a probe below) or be reviewed here.
WITHOUT_OWN_CLAUSE = frozenset(
    {
        "clinic_app.audit_append_system_unchecked_v1(text,text,inet,text,text,"
        "timestamp with time zone,jsonb,bytea)",
        "clinic_app.audit_append_system_unchecked_v2(text,text,inet,text,text,"
        "timestamp with time zone,jsonb,bytea)",
        "clinic_app.audit_append_unchecked_v1(text,text,inet,text,text,"
        "timestamp with time zone,jsonb,bytea)",
        "clinic_app.audit_append_unchecked_v2(text,text,inet,text,text,"
        "timestamp with time zone,jsonb,bytea)",
        "clinic_app.billing_payment_event_recorder(uuid)",
        "clinic_app.billing_revision_snapshot()",
        "clinic_app.billing_settlement_guard()",
        "clinic_app.configuration_guard()",
        "clinic_app.consent_guard()",
        "clinic_app.consent_session()",
        "clinic_app.ehr_assigned(uuid)",
        "clinic_app.ehr_care(uuid)",
        "clinic_app.ehr_history_care(uuid)",
        "clinic_app.load_current_user()",
        "clinic_app.questionnaire_receipt()",
        "clinic_app.retention_care(uuid,uuid)",
        "clinic_app.retention_guard()",
        "clinic_app.teleconsult_assigned(uuid)",
        "clinic_app.teleconsult_patient_match(uuid)",
        "clinic_app.user_organizations()",
    }
)


@dataclass(frozen=True)
class Probe:
    statement: str
    # Boolean gates answer false; the registry resolvers raise 42501 instead.
    raises: bool = False


# clinic_admin holds every permission probed here, so one real staff role
# satisfies each gate and the only change between calls is is_active.
PROBES = {
    "clinic_app.has_permission(text,uuid,uuid)": Probe(
        "SELECT clinic_app.has_permission('staff.clinic', %(clinic)s, NULL)"
    ),
    "clinic_app.user_has_org(uuid)": Probe(
        "SELECT clinic_app.user_has_org(%(organization)s)"
    ),
    "clinic_app.questionnaire_staff(uuid,text[])": Probe(
        "SELECT clinic_app.questionnaire_staff(%(clinic)s, ARRAY['clinic_admin'])"
    ),
    "clinic_app.waitlist_staff(uuid)": Probe(
        "SELECT clinic_app.waitlist_staff(%(clinic)s)"
    ),
    "clinic_app.list_active_clinic_physicians(uuid)": Probe(
        "SELECT %(physician)s IN (SELECT user_id FROM "
        "clinic_app.list_active_clinic_physicians(%(clinic)s))"
    ),
    "clinic_app.patient_registry_count(text,uuid,text,date)": Probe(
        "SELECT clinic_app.patient_registry_count("
        "'sintetico-kek', %(clinic)s, 'SINTETICO', NULL) = 0",
        raises=True,
    ),
    "clinic_app.patient_registry_page(text,uuid,text,date,integer,integer)": Probe(
        "SELECT NOT EXISTS (SELECT 1 FROM clinic_app.patient_registry_page("
        "'sintetico-kek', %(clinic)s, 'SINTETICO', NULL, 0, 10))",
        raises=True,
    ),
}


def _actor_readers() -> dict[str, bool]:
    """Map each clinic_app actor-setting reader to whether it reads is_active."""
    with transaction.atomic(), connection.cursor() as cursor:
        # regprocedure text is schema-qualified only off the search_path.
        cursor.execute("SET LOCAL search_path = pg_catalog")
        cursor.execute("""
            SELECT p.oid::regprocedure::text, l.lanname, p.prosrc, e.extname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            LEFT JOIN pg_depend d ON d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid AND d.deptype = 'e'
            LEFT JOIN pg_extension e ON e.oid = d.refobjid
            WHERE n.nspname = 'clinic_app'
        """)
        rows = cursor.fetchall()
    readers = {}
    for signature, language, source, extension in rows:
        if language not in {"sql", "plpgsql"}:
            # Native code is uninspectable; only extension members are accepted.
            assert extension is not None, (signature, language)
            continue
        parsed = references(source)
        assert parsed.opaque <= PARSEABLE_OPAQUE, (signature, parsed.opaque)
        if ACTOR_SETTING in parsed.settings:
            names = {name[-1] for name in parsed.names}
            readers[signature] = {"identity_user", "is_active"} <= names
    return readers


# Catalog read only: a rollback test avoids a transactional flush.
@pytest.mark.django_db
def test_every_actor_reader_with_an_is_active_clause_has_a_probe() -> None:
    readers = _actor_readers()
    assert {name for name, clause in readers.items() if clause} == set(PROBES)
    assert {name for name, clause in readers.items() if not clause} == (
        WITHOUT_OWN_CLAUSE
    )


def _staff_actor(graph: RbacGraph) -> UUID:
    actor = User.objects.create(username=f"sintetico-sql-inactive-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user=actor,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    return actor.pk


def _decide(graph: RbacGraph, actor: UUID, probe: Probe) -> bool:
    params = {
        "clinic": graph.clinic_a,
        "organization": graph.organization_a,
        "physician": graph.physician,
    }
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(graph.organization_a), str(actor)],
        )
        try:
            with transaction.atomic():
                cursor.execute(probe.statement, params)
                (row,) = cursor.fetchall()
        except ProgrammingError as error:
            if probe.raises and type(error.__cause__) is InsufficientPrivilege:
                return False
            raise
        (decision,) = row
        assert type(decision) is bool
        return decision


def _set_active(actor: UUID, *, active: bool) -> None:
    # Raw SQL as the owner: no model hook or Python actor check is involved.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET is_active = %s WHERE id = %s",
            [active, actor],
        )
        assert cursor.rowcount == 1


def test_sql_clause_refuses_inactive_actor(rbac_graph: RbacGraph) -> None:
    # One graph and one flush for all gates; a fresh actor per gate. The exact
    # map names every gate whose decision does not follow is_active alone.
    decisions = {}
    for function, probe in PROBES.items():
        actor = _staff_actor(rbac_graph)
        active = _decide(rbac_graph, actor, probe)
        _set_active(actor, active=False)
        inactive = _decide(rbac_graph, actor, probe)
        _set_active(actor, active=True)
        decisions[function] = (active, inactive, _decide(rbac_graph, actor, probe))
    assert decisions == dict.fromkeys(PROBES, (True, False, True))

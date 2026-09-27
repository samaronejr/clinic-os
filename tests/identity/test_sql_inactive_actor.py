"""Every SQL authority clause on ``is_active`` refuses an inactive actor itself.

Python (``current_context``) and tenant entry (``user_has_org`` through
``tenant_context``) also refuse inactive actors, so a probe that goes through
either never reaches the SQL clause. These probes bind the actor and tenant
GUCs directly as ``clinic_app`` and flip only ``identity_user.is_active``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from django.db import IntegrityError, ProgrammingError, connection, transaction
from psycopg.errors import CheckViolation, InsufficientPrivilege

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
_ATTRIBUTION = "attribution: stamps the actor id on the written row; decides nothing"
_PATIENT_ONLY = "patient-only: requires the actor setting to be absent"
# Actor readers that neither carry an is_active clause nor delegate to a
# checked helper. Every other actor reader must do one of the two.
JUSTIFIED = {
    "clinic_app.audit_append_system_unchecked_v1(text,text,inet,text,text,"
    "timestamp with time zone,jsonb,bytea)": _ATTRIBUTION,
    "clinic_app.audit_append_system_unchecked_v2(text,text,inet,text,text,"
    "timestamp with time zone,jsonb,bytea)": _ATTRIBUTION,
    "clinic_app.audit_append_unchecked_v1(text,text,inet,text,text,"
    "timestamp with time zone,jsonb,bytea)": _ATTRIBUTION,
    "clinic_app.audit_append_unchecked_v2(text,text,inet,text,text,"
    "timestamp with time zone,jsonb,bytea)": _ATTRIBUTION,
    "clinic_app.billing_revision_snapshot()": _ATTRIBUTION,
    "clinic_app.questionnaire_receipt()": _ATTRIBUTION,
    "clinic_app.consent_session()": _PATIENT_ONLY,
    "clinic_app.teleconsult_patient_match(uuid)": _PATIENT_ONLY,
    "clinic_app.load_current_user()": (
        "loader: returns is_active and every caller refuses an inactive row"
    ),
    "clinic_app.billing_payment_event_recorder(uuid)": (
        "recorded-actor binding for authenticated reconciliation; see billing README"
    ),
    "clinic_app.user_organizations()": (
        "post-login lookup, not a gate; user_has_org rechecks is_active"
    ),
}


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


@dataclass(frozen=True)
class Reader:
    own_clause: bool
    # One entry per called name: the signatures of all its overloads.
    callees: frozenset[frozenset[str]]


def _actor_readers() -> dict[str, Reader]:
    """Map each clinic_app actor-setting reader to its clause and callees."""
    with transaction.atomic(), connection.cursor() as cursor:
        # regprocedure text is schema-qualified only off the search_path.
        cursor.execute("SET LOCAL search_path = pg_catalog")
        cursor.execute("""
            SELECT p.oid::regprocedure::text, p.proname, l.lanname, p.prosrc,
                   e.extname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            LEFT JOIN pg_depend d ON d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid AND d.deptype = 'e'
            LEFT JOIN pg_extension e ON e.oid = d.refobjid
            WHERE n.nspname = 'clinic_app'
        """)
        rows = cursor.fetchall()
    overloads: dict[str, set[str]] = {}
    for signature, name, *_rest in rows:
        overloads.setdefault(name, set()).add(signature)
    readers = {}
    for signature, _name, language, source, extension in rows:
        if language not in {"sql", "plpgsql"}:
            # Native code is uninspectable; only extension members are accepted.
            assert extension is not None, (signature, language)
            continue
        parsed = references(source)
        assert parsed.opaque <= PARSEABLE_OPAQUE, (signature, parsed.opaque)
        if ACTOR_SETTING in parsed.settings:
            names = {name[-1] for name in parsed.names}
            callees = frozenset(
                frozenset(overloads[call[-1]])
                for call in parsed.calls
                if call[:-1] in {(), ("clinic_app",)} and call[-1] in overloads
            )
            readers[signature] = Reader(
                own_clause={"identity_user", "is_active"} <= names,
                callees=callees,
            )
    return readers


def _checked(readers: dict[str, Reader]) -> tuple[set[str], set[str]]:
    """Derive own-clause readers, then readers that call a checked helper."""
    own = {name for name, reader in readers.items() if reader.own_clause}
    checked = set(own)
    grew = True
    while grew:
        grew = False
        for name, reader in readers.items():
            # An edge counts only when every overload of the name is checked.
            if name not in checked and any(edge <= checked for edge in reader.callees):
                checked.add(name)
                grew = True
    return own, checked - own


# Catalog read only: a rollback test avoids a transactional flush.
@pytest.mark.django_db
def test_every_actor_reader_checks_is_active_or_is_justified() -> None:
    readers = _actor_readers()
    own, delegating = _checked(readers)
    assert own == set(PROBES)
    assert set(readers) - own - delegating == set(JUSTIFIED)
    # The executed delegation anchor below must stay in the derived set.
    assert "clinic_app.configuration_guard()" in delegating


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


CONFIGURATION_INSERT = """
    INSERT INTO clinic_app.identity_clinicconfiguration
        (id, version, display_name, contact_email, contact_phone, brand_token,
         reminder_hours, logo_png, created_at, clinic_id, organization_id,
         published_by_id, queue_quotas)
    VALUES (%s, %s, 'Sintetico', '', '', 'navy', 24, ''::bytea, now(), %s, %s,
            %s, '{}'::jsonb)
"""
GUARD_RAISE = re.compile(
    r"PL/pgSQL function (clinic_app\.)?configuration_guard\(\) line \d+ at RAISE"
)


def _publish(graph: RbacGraph, actor: UUID, version: int) -> CheckViolation | None:
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(graph.organization_a), str(actor)],
        )
        try:
            with transaction.atomic():
                cursor.execute(
                    CONFIGURATION_INSERT,
                    [uuid4(), version, graph.clinic_a, graph.organization_a, actor],
                )
        except IntegrityError as error:
            if type(error.__cause__) is CheckViolation:
                return error.__cause__
            raise
    return None


def test_configuration_guard_refuses_inactive_publisher(rbac_graph: RbacGraph) -> None:
    # Executed anchor for the delegating set: the BEFORE trigger's own
    # questionnaire_staff call must refuse, ahead of configuration_insert RLS.
    actor = _staff_actor(rbac_graph)
    assert _publish(rbac_graph, actor, 1) is None
    _set_active(actor, active=False)
    refusal = _publish(rbac_graph, actor, 2)
    _set_active(actor, active=True)
    assert _publish(rbac_graph, actor, 2) is None
    assert refusal is not None
    assert (refusal.sqlstate, refusal.diag.message_primary) == (
        "23514",
        "invalid configuration",
    )
    assert GUARD_RAISE.fullmatch(refusal.diag.context or ""), refusal.diag.context

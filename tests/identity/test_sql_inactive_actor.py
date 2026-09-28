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

import psycopg
import pytest
from apps.billing.services import create_invoice, issue_invoice
from apps.ehr.history import HistoryChange, save_history
from apps.ehr.models import SpecialtyTemplate
from apps.ehr.services import create_draft
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, ProgrammingError, connection, transaction
from psycopg.errors import InsufficientPrivilege
from sqlparse import tokens

from identity.authority_sql import _tokenize, references
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_teleconsult_sessions import _create as teleconsult_create
from renewal.test_teleconsult_sessions import seed as teleconsult_seed

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    language: str
    source: str
    # Called clinic_app function name -> the signatures of all its overloads.
    callees: dict[str, frozenset[str]]


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
            callees = {
                call[-1]: frozenset(overloads[call[-1]])
                for call in parsed.calls
                if call[:-1] in {(), ("clinic_app",)} and call[-1] in overloads
            }
            readers[signature] = Reader(
                own_clause={"identity_user", "is_active"} <= names,
                language=language,
                source=source,
                callees=callees,
            )
    return readers


# (identifier or literal, lower-cased text) for one lexical SQL token.
type Tok = tuple[bool, str]

# Clause keywords that end a WHERE or ON condition inside one query level.
_CLAUSE_END = frozenset({"where", "group by", "order by", "having", "limit", "window"})
_SET_OPERATORS = frozenset({"union", "union all", "intersect", "except"})
# The only non-identifier tokens a condition level may hold, besides the type
# after a :: cast. Anything else there (CASE/WHEN/END, BETWEEN, ARRAY[...],
# IS DISTINCT FROM, <, a comma...) can hide AND/OR that are not conjunctions of
# this level, so the whole level counts as unchecked.
_CONDITION_WORDS = frozenset(
    {"and", "or", "not", "exists", "is", "null", "not null", "true", "false"}
    | {"=", ".", "::"}
)


def _sql_tokens(source: str) -> list[Tok]:
    return [
        (kind is tokens.Name or kind in tokens.Literal, " ".join(value.lower().split()))
        for kind, value in _tokenize(source)
        if kind not in tokens.Whitespace and kind not in tokens.Comment
        if value != ";"
    ]


def _close(items: list[Tok], start: int) -> int:
    depth = 0
    for index in range(start, len(items)):
        depth += {"(": 1, ")": -1}.get(items[index][1], 0)
        if depth == 0:
            return index
    return -1


def _top(items: list[Tok]) -> list[tuple[int, Tok]]:
    """Tokens outside every parenthesis, with their positions."""
    depth, result = 0, []
    for index, item in enumerate(items):
        if item[1] == ")":
            depth -= 1
        elif item[1] == "(":
            depth += 1
        elif depth == 0:
            result.append((index, item))
    return result


def _understood(items: list[Tok]) -> bool:
    previous = ""
    for _index, (plain, value) in _top(items):
        if not (plain or value in _CONDITION_WORDS or previous == "::"):
            return False
        previous = value
    return True


def _split(items: list[Tok], word: str) -> list[list[Tok]]:
    parts, start = [], 0
    for index, (_plain, value) in _top(items):
        if value == word:
            parts.append(items[start:index])
            start = index + 1
    return [*parts, items[start:]]


def _requires(items: list[Tok], checked: set[str]) -> bool:
    """Whether the condition can hold only when a checked call holds.

    Every OR branch must require one, and one AND operand suffices. The rule is
    an allowlist: a level holding any token outside the understood grammar
    (identifiers, literals, calls, AND, OR, NOT, EXISTS, parenthesised groups,
    = and IS comparisons, :: casts) requires nothing, so an unrecognised shape
    fails the census rather than passing it.
    """
    if not _understood(items):
        return False
    disjuncts = _split(items, "or")
    if len(disjuncts) > 1:
        return all(_requires(branch, checked) for branch in disjuncts)
    conjuncts = _split(items, "and")
    if len(conjuncts) > 1:
        return any(_requires(operand, checked) for operand in conjuncts)
    return _atom_requires(items, checked)


def _atom_requires(items: list[Tok], checked: set[str]) -> bool:
    values = [value for _plain, value in items]
    if not items:
        return False
    if values[0] == "(" and _close(items, 0) == len(items) - 1:
        return _requires(items[1:-1], checked)
    if values[:2] == ["exists", "("] and _close(items, 1) == len(items) - 1:
        return _query_requires(items[2:-1], checked)
    call = items[2:] if values[:2] == ["clinic_app", "."] else items
    return (
        len(call) > 2
        and call[0][0]
        and call[0][1] in checked
        and call[1][1] == "("
        and _close(call, 1) == len(call) - 1
    )


def _query_requires(items: list[Tok], checked: set[str]) -> bool:
    """A subquery requires a check through its WHERE or inner-join ON condition."""
    top = [(index, value) for index, (_plain, value) in _top(items)]
    words = {value for _index, value in top}
    if items[:1] != [(False, "select")] or words & _SET_OPERATORS:
        return False
    inner = all(value in {"join", "inner join"} for value in words if "join" in value)
    conditions = []
    for index, value in top:
        if value == "where" or (value == "on" and inner):
            end = next(
                (
                    later
                    for later, word in top
                    if later > index
                    and (word in _CLAUSE_END or word == "on" or "join" in word)
                ),
                len(items),
            )
            conditions.append(items[index + 1 : end])
    return any(_requires(condition, checked) for condition in conditions)


def _delegates(reader: Reader, checked: set[str]) -> bool:
    """SQL bodies must require a checked call on every branch; plpgsql, a call.

    plpgsql control flow is not analysed here: every plpgsql delegator is a
    trigger guard whose own refusal is executed by GUARD_PROBES.
    """
    names = {name for name, edge in reader.callees.items() if edge <= checked}
    if reader.language != "sql":
        return bool(names)
    items = _sql_tokens(reader.source)
    if items[:1] != [(False, "select")] or any(
        value == "from" for _index, (_plain, value) in _top(items)
    ):
        return False
    return _requires(items[1:], names)


def _checked(readers: dict[str, Reader]) -> tuple[set[str], set[str]]:
    """Derive own-clause readers, then readers that delegate to them."""
    own = {name for name, reader in readers.items() if reader.own_clause}
    checked = set(own)
    grew = True
    while grew:
        grew = False
        for name, reader in readers.items():
            # An edge counts only when every overload of the name is checked.
            if name not in checked and _delegates(reader, checked):
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
    # Every derived delegator is executed below; a new one needs a probe.
    assert set(DELEGATE_PROBES) | set(GUARD_PROBES) == delegating


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


def _decide(
    graph: RbacGraph,
    actor: UUID,
    statement: str,
    params: Mapping[str, UUID | int],
    *,
    raises: bool = False,
) -> bool:
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(graph.organization_a), str(actor)],
        )
        try:
            with transaction.atomic():
                cursor.execute(statement, params)
                (row,) = cursor.fetchall()
        except ProgrammingError as error:
            if raises and type(error.__cause__) is InsufficientPrivilege:
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
    params = {
        "clinic": rbac_graph.clinic_a,
        "organization": rbac_graph.organization_a,
        "physician": rbac_graph.physician,
    }
    decisions = {}
    for function, probe in PROBES.items():
        actor = _staff_actor(rbac_graph)
        phases = []
        for active in (True, False, True):
            _set_active(actor, active=active)
            phases.append(
                _decide(rbac_graph, actor, probe.statement, params, raises=probe.raises)
            )
        decisions[function] = tuple(phases)
    assert decisions == dict.fromkeys(PROBES, (True, False, True))


# Boolean delegators, executed as the assigned physician of one seeded
# appointment, encounter, enrollment and teleconsult session.
DELEGATE_PROBES = {
    "clinic_app.ehr_assigned(uuid)": "SELECT clinic_app.ehr_assigned(%(encounter)s)",
    "clinic_app.ehr_care(uuid)": "SELECT clinic_app.ehr_care(%(encounter)s)",
    "clinic_app.ehr_history_care(uuid)": (
        "SELECT clinic_app.ehr_history_care(%(encounter)s)"
    ),
    "clinic_app.retention_care(uuid,uuid)": (
        "SELECT clinic_app.retention_care(%(clinic)s, %(patient)s)"
    ),
    "clinic_app.teleconsult_assigned(uuid)": (
        "SELECT clinic_app.teleconsult_assigned(%(session)s)"
    ),
}


@dataclass(frozen=True)
class GuardProbe:
    insert: str
    # The guard's own RAISE message; RLS or another trigger raises differently.
    refusal: str
    # Owner query for the latest version the insert must follow, if versioned.
    latest: str | None = None


GUARD_PROBES = {
    "clinic_app.configuration_guard()": GuardProbe(
        "INSERT INTO clinic_app.identity_clinicconfiguration (id, version, "
        "display_name, contact_email, contact_phone, brand_token, reminder_hours, "
        "logo_png, created_at, clinic_id, organization_id, published_by_id, "
        "queue_quotas) VALUES (%(id)s, %(version)s, 'Sintetico', '', '', 'navy', "
        "24, ''::bytea, now(), %(clinic)s, %(organization)s, %(actor)s, "
        "'{}'::jsonb)",
        "invalid configuration",
        "SELECT coalesce(max(version), 0) FROM clinic_app.identity_clinicconfiguration"
        " WHERE clinic_id = %(clinic)s",
    ),
    "clinic_app.consent_guard()": GuardProbe(
        "INSERT INTO clinic_app.consent_consenttext (id, purpose, version, "
        "language, digest, created_at, clinic_id, organization_id, "
        "published_by_id, text) VALUES (%(id)s, 'teleconsultation', %(version)s, "
        "'pt-BR', repeat('0', 64), now(), %(clinic)s, %(organization)s, "
        "%(actor)s, convert_to('Sintetico', 'UTF8'))",
        "invalid consent publication",
        "SELECT coalesce(max(version), 0) FROM clinic_app.consent_consenttext"
        " WHERE clinic_id = %(clinic)s AND purpose = 'teleconsultation'",
    ),
    "clinic_app.retention_guard()": GuardProbe(
        "INSERT INTO clinic_app.retention_retentionpolicy (id, record_class, "
        "version, state, retention_days, created_at, clinic_id, organization_id, "
        "proposed_by_id) VALUES (%(id)s, 'ehr.encounter', %(version)s, "
        "'proposed', 365, now(), %(clinic)s, %(organization)s, %(actor)s)",
        "invalid policy binding",
        "SELECT coalesce(max(version), 0) FROM clinic_app.retention_retentionpolicy"
        " WHERE clinic_id = %(clinic)s AND record_class = 'ehr.encounter'",
    ),
    "clinic_app.billing_settlement_guard()": GuardProbe(
        "INSERT INTO clinic_app.billing_settlement (id, confirmation_reference, "
        "amount_minor, currency, confirmed_by_id, confirmed_at, invoice_id, "
        "organization_id) VALUES (%(id)s, %(id)s, %(amount)s, 'BRL', %(actor)s, "
        "now(), %(invoice)s, %(organization)s)",
        "invalid confirmed settlement",
    ),
}
GUARD_RAISE = re.compile(
    r"PL/pgSQL function (?:clinic_app\.)?(\w+)\(\) line \d+ at RAISE"
)
AMOUNT = 12345

type Outcome = str | tuple[str | None, str | None, str | None]


def _insert(
    graph: RbacGraph, actor: UUID, probe: GuardProbe, params: Mapping[str, UUID | int]
) -> Outcome:
    """Return "inserted" or the refusal's sqlstate, message and raising guard."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "set_config('app.current_user_id', %s, true)",
            [str(graph.organization_a), str(actor)],
        )
        try:
            with transaction.atomic():
                cursor.execute(probe.insert, params)
        except DatabaseError as error:
            cause = error.__cause__
            if not isinstance(cause, psycopg.Error):
                raise
            raised = GUARD_RAISE.search(cause.diag.context or "")
            return (
                cause.sqlstate,
                cause.diag.message_primary,
                f"clinic_app.{raised.group(1)}()" if raised else None,
            )
    return "inserted"


def _open_invoices(graph: RbacGraph, patient: UUID) -> list[UUID]:
    invoices = []
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        for _ in range(2):
            invoice = create_invoice(
                clinic_id=graph.clinic_a,
                patient_id=patient,
                amount_minor=AMOUNT,
                idempotency_key=uuid4(),
            )
            issue_invoice(
                clinic_id=graph.clinic_a, invoice_id=invoice.pk, expected_revision=1
            )
            invoices.append(invoice.pk)
    return invoices


def test_delegating_gates_refuse_inactive_actor(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, encounter, _consent, _patient, _manager = teleconsult_seed(graph)
    session = teleconsult_create(graph, encounter)
    # A history author reaches ehr_history_care's second branch, whose own
    # questionnaire_staff call is the only refusal once ehr_care is false.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        save_history(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            change=HistoryChange(
                kind="problem",
                expected_revision=0,
                state="documented",
                description="Problema sintetico",
                status="active",
                reason="Registro inicial",
            ),
        )
    # An authored draft reaches the authored-version branch of ehr_care and
    # retention_care, which the appointment branch would otherwise satisfy.
    with owner_context(graph.organization_a):
        template = SpecialtyTemplate.objects.get(clinic_id=graph.clinic_a)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        create_draft(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
        )
    scope: dict[str, UUID] = {
        "clinic": graph.clinic_a,
        "organization": graph.organization_a,
        "encounter": encounter.pk,
        "patient": appointment.patient_id,
        "session": session.pk,
    }
    invoices = _open_invoices(graph, appointment.patient_id)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        latest = {}
        for function, probe in GUARD_PROBES.items():
            if probe.latest is not None:
                cursor.execute(probe.latest, scope)
                (latest[function],) = cursor.fetchone()
    manager = _staff_actor(graph)
    rows: dict[str, list[bool | Outcome]] = {
        name: [] for name in [*DELEGATE_PROBES, *GUARD_PROBES]
    }
    # The inactive and reactivated attempts differ only in the new row id.
    for phase, active in enumerate((True, False, True)):
        _set_active(graph.physician, active=active)
        _set_active(manager, active=active)
        for function, statement in DELEGATE_PROBES.items():
            rows[function].append(_decide(graph, graph.physician, statement, scope))
        for function, probe in GUARD_PROBES.items():
            params = {
                **scope,
                "id": uuid4(),
                "actor": manager,
                "amount": AMOUNT,
                "invoice": invoices[min(phase, 1)],
                "version": latest.get(function, 0) + min(phase, 1) + 1,
            }
            rows[function].append(_insert(graph, manager, probe, params))
    # One exact map, so every gate that does not follow is_active is named.
    assert {name: tuple(row) for name, row in rows.items()} == {
        **dict.fromkeys(DELEGATE_PROBES, (True, False, True)),
        **{
            name: ("inserted", ("23514", probe.refusal, name), "inserted")
            for name, probe in GUARD_PROBES.items()
        },
    }

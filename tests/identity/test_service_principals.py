"""Todo 7: real-login machine authority, never a forged staff actor."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg
import pytest
from apps.comms.tasks import execute_operation as execute_operation_task
from apps.core.integration import (
    IntegrationContextError,
    OperationRequest,
    enqueue_operation,
)
from apps.ehr.services import publish_template
from apps.identity.current_context import CurrentActorError
from apps.identity.models import (
    ServicePrincipal,
    ServicePrincipalGrant,
    User,
    UserClinicRole,
)
from apps.scheduling.models import AvailabilityBlock
from apps.tenancy.db import (
    ServicePrincipalAccessDeniedError,
    TenantAccessDeniedError,
    TenantTransactionNestingError,
    clear_connection_tenant_gucs,
    service_principal_context,
    tenant_context,
)
from django.db import (
    DatabaseError,
    IntegrityError,
    connection,
    connections,
    transaction,
)
from psycopg import sql

from identity.authority_catalog import Catalog, Reads
from identity.authority_sql import references
from identity.machine_gate import (
    NOT_GATED,
    SETTING_WRITE,
    UNBOUND_CLINIC,
    UNGATED_UUID,
    member_gate,
    setting_writes,
)
from identity.nonstaff_states import (
    ReplayScope,
    StaffState,
    add_authority_backstop,
    backstop_states,
    set_memberships,
)
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from uuid import UUID

    from pytest_django.plugin import DjangoDbBlocker

    from identity.authority_catalog import Node
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})
# Census contract for the machine_principal SQL kind (test_sql_guard_inventory):
# every registry member carries exactly these executed oracles.
MACHINE_ORACLES = [
    "test_machine_sql_closure_reads_no_staff_authority",
    "test_staff_state_never_grants_machine_authority",
]
REGISTRY = json.loads(Path(__file__).with_name("legacy_guards.json").read_text())
# Structural machine_principal rule (machine_violations): the reviewed refusal
# helper refuses any non-empty actor GUC; no other member may read the actor.
REFUSAL_HELPER = "clinic_app.principal_scope"
ACTOR_GUC = "app.current_user_id"
# The reviewed helper is pinned exactly: sha256 of pg_get_functiondef plus
# owner and ACL. The only normalization is pg_get_functiondef's own: it renders
# the header (signature, language, volatility, SECURITY DEFINER, SET
# search_path, strictness, cost) canonically from pg_proc, so the pin does not
# depend on how a migration spelled it. The body is prosrc verbatim and no text
# is rewritten, so every byte, including whitespace inside literals, counts.
# Changing principal_scope means re-reviewing it and updating this pin.
REFUSAL_HELPER_DIGEST = (
    "5d1317b75f31ab4987abf387194cced1b4c9a2d1f866baef1d9152a3aea2a28a"
)
# Everything the pinned helper's closure may read; any view is refused.
REFUSAL_HELPER_RELATIONS = frozenset(
    {
        "clinic_app.identity_serviceprincipal",
        "clinic_app.identity_serviceprincipalgrant",
    }
)
REFUSAL_HELPER_SETTINGS = frozenset(
    {"app.current_patient_session", "app.current_user_id", "transaction_isolation"}
)
# The only settings any other member may read, with the helper sealed: its own
# machine scope. Actor, patient and invented settings all fail closed. This is
# a cross-check; the rule is the principal_scope gate (machine_gate), because
# these two settings are themselves caller-settable on the agent login.
MEMBER_SETTINGS = frozenset({"app.current_principal", "app.current_tenant"})
MACHINE_SQL_OPAQUE = {"opaque function pg_catalog.pg_has_role"}
# Machine members and their helpers are read-only, because a refused call must
# be side-effect free and PostgreSQL does not order a SQL member's AND
# conjuncts. The rule is an allowlist of volatility: every function in a
# closure (member, helper, native) must be IMMUTABLE or STABLE. PostgreSQL
# then refuses INSERT/UPDATE/DELETE/MERGE, SELECT FOR UPDATE/SHARE,
# data-modifying CTEs and every utility command (SET, NOTIFY, LOCK, COPY...)
# inside sql/plpgsql bodies, and volatile natives (nextval, setval,
# pg_advisory*, pg_notify, lo_*, set_config) cannot be reached. Native
# functions outside pg_catalog are already opaque. The only exception is
# VOLATILE_GATES below.
READ_ONLY_VOLATILITY = frozenset({"i", "s"})
# The gate functions stay VOLATILE, like staff has_permission: each call takes
# a fresh READ COMMITTED snapshot, so a revocation committed while a cursor or
# a long statement is running stops its next row (review round 9, D1-r9;
# test_revocation_stops_*). PostgreSQL does not keep a VOLATILE body
# read-only, so each gate is admitted only (a) with its reviewed definition,
# pinned like REFUSAL_HELPER_DIGEST, because a statement such as FOR SHARE,
# LOCK or NOTIFY in a VOLATILE body is invisible to the write rule, and (b)
# while its own closure passes the write, setting and volatile-callee rules,
# with only other admitted gates exempt (_admitted_gates). The executed
# snapshot check runs both. The set is exactly the refusal helper plus the
# functions the clinic_agent RLS policies call
# (test_volatile_gate_exception_is_exact). Either gate VOLATILE is enough for
# the revocation tests: principal_has calls principal_scope, and both read the
# same principal and grant rows (the grant CHECK allows appointment.read only),
# so one fresh snapshot refuses. The tests fail when both are STABLE (the
# D1-r9 configuration); either one alone is refused by its pin here and by
# the resolver posture pin.
VOLATILE_GATES: dict[str, tuple[str, str]] = {
    REFUSAL_HELPER: (REFUSAL_HELPER + "(uuid,uuid)", REFUSAL_HELPER_DIGEST),
    "clinic_app.principal_has": (
        "clinic_app.principal_has(text,uuid)",
        "60aa50c81ffd438d6075eee2b592bafcab0ef8bfade6d211b80eaf96902744d8",
    ),
}
# Builtins labelled STABLE that still assign a transaction id.
XID_ASSIGNING = frozenset({"pg_catalog.txid_current", "pg_catalog.pg_current_xact_id"})
WRITES_RELATION = "writes a relation"
VOLATILE_CALL = "calls a volatile function"
# Cross-check only (the rule is machine_violations). Actor GUC cells: cleared;
# the matrix actor; other staff; every pooled uuid ("pooled"); non-uuid values
# ("malformed"); and each call's own argument values ("own_argument").
ACTOR_GUCS = (
    "cleared",
    "forged",
    "mismatched",
    "pooled",
    "malformed",
    "own_argument",
)


def machine_members(registry: dict[str, Any] = REGISTRY) -> list[str]:
    """Every SQL function the census registry labels machine_principal."""
    return sorted(
        name
        for name, entry in registry["sql_guards"].items()
        if entry["kind"] == "machine_principal"
    )


def _argument_types(members: list[str]) -> dict[str, list[str]]:
    """Live argument types; a registered member missing from the catalog fails."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT n.nspname || '.' || p.proname, "
            "ARRAY(SELECT format_type(t, NULL) FROM unnest(p.proargtypes) t) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname || '.' || p.proname = ANY(%s)",
            [members],
        )
        rows = cursor.fetchall()
    found = {name: list(types) for name, types in rows}
    assert sorted(found) == sorted(members), (members, sorted(found))
    assert len(rows) == len(members), "overloaded machine member"
    return found


def _machine_vectors(
    types: dict[str, list[str]], pools: dict[str, list[UUID | str]]
) -> dict[str, list[tuple[UUID | str, ...]]]:
    """Every combination of pooled arguments; an unpooled type fails closed."""
    vectors = {}
    for name, argument_types in types.items():
        for argument_type in argument_types:
            assert pools.get(argument_type), (name, "no argument pool", argument_type)
        vectors[name] = list(product(*(pools[t] for t in argument_types)))
    return vectors


def _call(name: str, types: list[str]) -> str:
    arguments = ", ".join(f"%s::{t}" for t in types)
    return f"SELECT {name}({arguments})"


def _granted(value: object) -> bool:
    return value is not None and value is not False


def _assert_every_member_executed(
    members: list[str], decisions: Mapping[tuple[str, str, str], tuple[object, ...]]
) -> None:
    for name in members:
        executed = [
            values for (member, _t, _a), values in decisions.items() if member == name
        ]
        assert len(executed) == 2 * len(ACTOR_GUCS), (name, "not executed")
        assert all(
            values
            for (member, _t, actor_guc), values in decisions.items()
            if member == name and actor_guc != "own_argument"
        ), (name, "no executed decisions")


@pytest.fixture(scope="session", autouse=True)
def agent_login(
    django_db_setup: None, django_db_blocker: DjangoDbBlocker
) -> Iterator[None]:
    """Use a real isolated agent login even when the optional runtime alias is off."""
    secret = secrets.token_urlsafe(32)
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        old = admin.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname='clinic_agent'"
        ).fetchone()
        assert old is not None
        admin.execute(
            sql.SQL("ALTER ROLE clinic_agent PASSWORD {}").format(sql.Literal(secret))
        )
        with django_db_blocker.unblock():
            agent = connections["agent"]
            agent.close()
            agent.settings_dict["PASSWORD"] = secret
        try:
            yield
        finally:
            with django_db_blocker.unblock():
                connections["agent"].close()
            admin.execute(
                sql.SQL("ALTER ROLE clinic_agent PASSWORD {}").format(
                    sql.Literal(old[0])
                )
            )


@pytest.fixture
def principal(rbac_graph: RbacGraph) -> ServicePrincipal:
    with owner_context(rbac_graph.organization_a):
        result = ServicePrincipal.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            name="sintetico-availability",
            db_identity="clinic_agent",
            purpose="availability",
        )
        ServicePrincipalGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            principal=result,
            permission="appointment.read",
            subject_scope="clinic",
        )
    return result


def _seed_availability(graph: RbacGraph) -> set[UUID]:
    start = datetime(2031, 1, 1, 12, tzinfo=UTC)
    result = set()
    for offset, (org, clinic) in enumerate(
        (
            (graph.organization_a, graph.clinic_a),
            (graph.organization_a, graph.clinic_b),
            (graph.organization_b, graph.clinic_c),
        )
    ):
        with owner_context(org):
            block = AvailabilityBlock.objects.create(
                organization_id=org,
                clinic_id=clinic,
                practitioner_id=graph.physician
                if org == graph.organization_a
                else graph.shared_user,
                start_at=start + timedelta(days=offset),
                end_at=start + timedelta(days=offset, hours=1),
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            )
            if clinic == graph.clinic_a:
                result.add(block.pk)
    return result


def _visible() -> set[UUID]:
    return set(AvailabilityBlock.objects.using("agent").values_list("pk", flat=True))


def test_real_login_reads_only_exact_granted_clinic(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    expected = _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT session_user, current_user, "
                "NULLIF(current_setting('app.current_user_id', true), '')"
            )
            assert cursor.fetchone() == ("clinic_agent", "clinic_agent", None)
        assert _visible() == expected
        assert (
            not AvailabilityBlock.objects.using("agent")
            .filter(clinic_id=rbac_graph.clinic_b)
            .exists()
        )
        assert (
            not AvailabilityBlock.objects.using("agent")
            .filter(clinic_id=rbac_graph.clinic_c)
            .exists()
        )
    assert _visible() == set()


@pytest.mark.parametrize("selector", ["unknown", "same_org", "foreign_org"])
def test_wrong_principal_or_clinic_is_indistinguishable(
    principal: ServicePrincipal, rbac_graph: RbacGraph, selector: str
) -> None:
    principal_id = uuid4() if selector == "unknown" else principal.pk
    clinic = {
        "unknown": principal.clinic_id,
        "same_org": rbac_graph.clinic_b,
        "foreign_org": rbac_graph.clinic_c,
    }[selector]
    with (
        pytest.raises(
            ServicePrincipalAccessDeniedError, match="service principal access denied"
        ),
        service_principal_context(principal_id=principal_id, clinic_id=clinic),
    ):
        pytest.fail("unauthorized context entered")


@pytest.mark.parametrize("target", ["principal", "grant"])
def test_revocation_is_visible_inside_existing_context(
    principal: ServicePrincipal, rbac_graph: RbacGraph, target: str
) -> None:
    expected = _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        assert _visible() == expected
        # A distinct owner connection commits while the agent transaction is open.
        with owner_context(principal.organization_id):
            if target == "principal":
                ServicePrincipal.objects.filter(pk=principal.pk).update(active=False)
            else:
                ServicePrincipalGrant.objects.filter(principal=principal).update(
                    active=False
                )
        assert _visible() == set()
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("revoked context entered")


def _seed_own_blocks(graph: RbacGraph, count: int) -> set[UUID]:
    """Blocks in the principal's clinic, all visible to the agent."""
    start = datetime(2032, 1, 1, 12, tzinfo=UTC)
    with owner_context(graph.organization_a):
        return {
            AvailabilityBlock.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                practitioner_id=graph.physician,
                start_at=start + timedelta(days=day),
                end_at=start + timedelta(days=day, hours=1),
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            ).pk
            for day in range(count)
        }


def _revoke(principal: ServicePrincipal, target: str) -> None:
    """Revoke from a distinct owner connection and commit."""
    with owner_context(principal.organization_id):
        if target == "principal":
            ServicePrincipal.objects.filter(pk=principal.pk).update(active=False)
        else:
            ServicePrincipalGrant.objects.filter(principal=principal).update(
                active=False
            )


def _agent_in_scope(principal: ServicePrincipal) -> psycopg.Connection[Any]:
    """A raw agent-login connection inside an open machine transaction."""
    agent = psycopg.connect(**connections["agent"].get_connection_params())
    agent.execute(
        "SELECT set_config('app.current_principal', %s, true),"
        " set_config('app.current_tenant', %s, true)",
        [str(principal.pk), str(principal.organization_id)],
    )
    return agent


@pytest.mark.parametrize("target", ["principal", "grant"])
def test_revocation_stops_an_open_cursor_on_the_agent_login(
    principal: ServicePrincipal, rbac_graph: RbacGraph, target: str
) -> None:
    """Review round 9 (D1-r9): the agent_grant policy calls the VOLATILE
    principal_has per row, which takes a fresh snapshot, so rows fetched from
    an open cursor after a revocation commits are refused. (Either gate alone
    STABLE is masked by the other; see VOLATILE_GATES.)"""
    own = _seed_own_blocks(rbac_graph, 3)
    with _agent_in_scope(principal) as agent, agent.cursor() as cursor:
        cursor.execute(
            "DECLARE machine_rows NO SCROLL CURSOR FOR"
            " SELECT id FROM clinic_app.scheduling_availabilityblock"
        )
        cursor.execute("FETCH 1 FROM machine_rows")
        first = cursor.fetchall()
        _revoke(principal, target)
        cursor.execute("FETCH ALL FROM machine_rows")
        rest = cursor.fetchall()
        agent.rollback()
    # Anti-vacuity: the grant was live for the first row, and two rows remained.
    assert [row[0] for row in first] in [[block] for block in own]
    assert rest == []


# Advisory lock keys for the statement handshake (lane test database only).
GATE_LOCK, ROW_LOCK = 7_314_001, 7_314_002


@pytest.mark.parametrize("target", ["principal", "grant"])
def test_revocation_stops_the_rest_of_a_running_statement(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    target: str,
) -> None:
    """Review round 9 (D1-r9): one SELECT is held after its first row passed
    the agent_grant policy. Its target list, evaluated per row after the RLS
    check, releases ROW_LOCK and then waits on GATE_LOCK. The test takes
    ROW_LOCK (so the first row has been checked), commits the revocation,
    releases GATE_LOCK, and the statement's remaining rows are refused.
    Bounded by lock_timeout and the future's timeout; no timer waits."""
    own = _seed_own_blocks(rbac_graph, 3)
    with (
        psycopg.connect(superuser_database_url, autocommit=True) as gate,
        psycopg.connect(superuser_database_url, autocommit=True) as watcher,
        _agent_in_scope(principal) as agent,
    ):
        gate.execute("SELECT pg_advisory_lock(%s)", [GATE_LOCK])
        agent.execute("SELECT pg_advisory_lock(%s)", [ROW_LOCK])

        def statement() -> list[tuple[Any, ...]]:
            return agent.execute(
                "SELECT b.id, pg_advisory_unlock(%s),"
                " pg_advisory_xact_lock_shared(%s)"
                " FROM clinic_app.scheduling_availabilityblock b",
                [ROW_LOCK, GATE_LOCK],
            ).fetchall()

        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(statement)
            watcher.execute("SET lock_timeout = '60s'")
            watcher.execute("SELECT pg_advisory_lock(%s)", [ROW_LOCK])
            _revoke(principal, target)
            gate.execute("SELECT pg_advisory_unlock(%s)", [GATE_LOCK])
            rows = running.result(timeout=60)
        agent.rollback()
    # Anti-vacuity: the first row was granted, and two rows remained.
    assert [row[0] for row in rows] in [[block] for block in own]


def test_principal_context_refuses_nesting(principal: ServicePrincipal) -> None:
    with (
        transaction.atomic(),
        pytest.raises(TenantTransactionNestingError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("nested staff transaction")
    with (
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
        pytest.raises(TenantTransactionNestingError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("nested agent transaction")


@pytest.mark.parametrize("guc", ["app.current_user_id", "app.current_patient_session"])
def test_forged_human_context_never_authorizes_machine_reads(
    principal: ServicePrincipal, rbac_graph: RbacGraph, guc: str
) -> None:
    _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s,%s,true)", [guc, str(rbac_graph.physician)]
            )
        assert _visible() == set()


@contextmanager
def _agent_as_default() -> Iterator[None]:
    original = connections["default"]
    connections["default"] = connections["agent"]
    try:
        yield
    finally:
        connections["default"] = original


def test_forged_physician_cannot_call_ehr_service(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    # A physician-owner really can publish this template as a human. The same
    # UUID on the machine login must fail before any EHR read/write or audit.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=principal.clinic_id,
            user_id=rbac_graph.physician,
            role="owner",
        )
    prompts: dict[str, str] = dict.fromkeys(
        ("subjective", "objective", "assessment", "plan"), "Sintetico"
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert (
            publish_template(
                clinic_id=principal.clinic_id,
                key="synthetic-human",
                title="Sintetico",
                prompts=prompts,
            ).key
            == "synthetic-human"
        )
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(rbac_graph.physician)],
            )
        with _agent_as_default(), pytest.raises(CurrentActorError):
            publish_template(
                clinic_id=principal.clinic_id,
                key="synthetic",
                title="Sintetico",
                prompts=prompts,
            )


@pytest.mark.parametrize(
    "table",
    [
        "audit_event",
        "tenancy_tenantdatakey",
        "identity_user",
        "identity_serviceprincipal",
        "ehr_encounter",
    ],
)
def test_agent_cannot_read_ungranted_sensitive_tables(
    principal: ServicePrincipal, table: str
) -> None:
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with (
            pytest.raises(DatabaseError) as error,
            transaction.atomic(using="agent"),
            connections["agent"].cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("SELECT * FROM clinic_app.{}").format(sql.Identifier(table))
            )
        assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize(
    "permission",
    [
        "clinical.finalize",
        "prescription.sign",
        "refund.approve",
        "*",
        "appointment.read; DROP TABLE x",
    ],
)
def test_machine_grants_reject_unsafe_permissions(
    principal: ServicePrincipal, permission: str
) -> None:
    with (
        owner_context(principal.organization_id),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=principal,
            permission=permission,
            subject_scope="clinic",
        )


def test_login_binding_is_unique_and_history_cannot_be_rewritten(
    principal: ServicePrincipal,
) -> None:
    with owner_context(principal.organization_id):
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.create(
                organization_id=principal.organization_id,
                clinic_id=principal.clinic_id,
                name="sintetico-other",
                db_identity="clinic_agent",
                purpose="availability",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).update(purpose="other")
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).delete()
        ServicePrincipal.objects.filter(pk=principal.pk).update(active=False)
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).update(active=True)


def test_agent_cannot_switch_to_staff_or_resolver(principal: ServicePrincipal) -> None:
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        for role in ("clinic_app", "clinic_owner", "clinic_resolver"):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(using="agent"),
                connections["agent"].cursor() as cursor,
            ):
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))


def test_caller_cannot_select_another_registered_login(
    principal: ServicePrincipal,
) -> None:
    with owner_context(principal.organization_id):
        other = ServicePrincipal.objects.create(
            organization_id=principal.organization_id,
            clinic_id=principal.clinic_id,
            name="sintetico-other",
            db_identity="clinic_agent_other",
            purpose="availability",
        )
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=other,
            permission="appointment.read",
            subject_scope="clinic",
        )
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(principal_id=other.pk, clinic_id=other.clinic_id),
    ):
        pytest.fail("a different login's identity was accepted")


def test_raw_guc_forgery_cannot_replace_a_clinic_grant(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [str(rbac_graph.organization_b)],
            )
        assert _visible() == set()


@pytest.mark.parametrize("subject_scope", ["*", "organization", "patient", ""])
def test_unsupported_subject_scope_is_not_a_wildcard(
    principal: ServicePrincipal,
    subject_scope: str,
) -> None:
    with (
        owner_context(principal.organization_id),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=principal,
            permission="appointment.read",
            subject_scope=subject_scope,
        )


def test_scope_foreign_keys_cannot_cross_organizations(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with owner_context(rbac_graph.organization_b):
        with pytest.raises(IntegrityError) as grant_error, transaction.atomic():
            ServicePrincipalGrant.objects.create(
                organization_id=rbac_graph.organization_b,
                principal=principal,
                permission="appointment.read",
                subject_scope="clinic",
                active=False,
            )
        assert isinstance(
            grant_error.value.__cause__, psycopg.errors.ForeignKeyViolation
        )
        assert (
            grant_error.value.__cause__.diag.constraint_name
            == "identity_principal_grant_org_fk"
        )
        with pytest.raises(IntegrityError) as principal_error, transaction.atomic():
            ServicePrincipal.objects.create(
                organization_id=rbac_graph.organization_b,
                clinic_id=rbac_graph.clinic_a,
                name="sintetico-cross",
                db_identity="clinic_agent_cross",
                purpose="availability",
            )
        assert isinstance(
            principal_error.value.__cause__, psycopg.errors.ForeignKeyViolation
        )
        assert (
            principal_error.value.__cause__.diag.constraint_name
            == "identity_principal_clinic_fk"
        )


def test_principal_helper_fails_closed_on_malformed_or_ungranted_input(
    principal: ServicePrincipal,
) -> None:
    with (
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
        connections["agent"].cursor() as cursor,
    ):
        for permission in (
            "clinical.finalize",
            "*",
            "SINTETICO-SENTINELA-INJECTION",
        ):
            cursor.execute(
                "SELECT clinic_app.principal_has(%s,%s)",
                [permission, principal.clinic_id],
            )
            assert cursor.fetchone() == (False,)
        cursor.execute("SELECT set_config('app.current_principal','malformed',true)")
        cursor.execute(
            "SELECT clinic_app.principal_has('appointment.read',%s)",
            [principal.clinic_id],
        )
        assert cursor.fetchone() == (False,)


def test_poisoned_session_and_exception_exit_clear_all_machine_context(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,false), "
            "set_config('app.current_principal',%s,false), "
            "set_config('app.current_tenant',%s,false), "
            "set_config('app.current_patient_session',%s,false)",
            [
                str(rbac_graph.physician),
                str(uuid4()),
                str(rbac_graph.organization_b),
                str(uuid4()),
            ],
        )
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("mixed human context was accepted")
    message = "synthetic rollback"
    with (
        pytest.raises(RuntimeError, match=message),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        raise RuntimeError(message)
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_principal',true),''), "
            "NULLIF(current_setting('app.current_tenant',true),''), "
            "NULLIF(current_setting('app.current_user_id',true),''), "
            "NULLIF(current_setting('app.current_patient_session',true),'')"
        )
        assert cursor.fetchone() == (None, None, None, None)


def test_machine_context_cannot_borrow_stale_staff_outbox_authority(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with runtime_role():
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,false), "
                "set_config('app.current_tenant',%s,false)",
                [str(rbac_graph.physician), str(rbac_graph.organization_a)],
            )
        try:
            with (
                service_principal_context(
                    principal_id=principal.pk, clinic_id=principal.clinic_id
                ),
                pytest.raises(IntegrationContextError),
            ):
                enqueue_operation(
                    OperationRequest(
                        channel="email",
                        provider="synthetic",
                        clinic_id=principal.clinic_id,
                        subject_type="synthetic.action",
                        subject_id=uuid4(),
                        idempotency_key=uuid4(),
                    )
                )
        finally:
            clear_connection_tenant_gucs()


def test_machine_login_cannot_install_staff_context(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with (
        _agent_as_default(),
        pytest.raises(TenantAccessDeniedError),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        pytest.fail("machine login installed a staff context")
    with connections["agent"].cursor() as cursor:
        cursor.execute("SELECT NULLIF(current_setting('app.current_user_id',true),'')")
        assert cursor.fetchone() == (None,)


def test_repeatable_read_cannot_hide_committed_revocation(
    principal: ServicePrincipal,
) -> None:
    agent = connections["agent"]
    with agent.cursor() as cursor:
        cursor.execute("SET default_transaction_isolation='repeatable read'")
    try:
        with (
            pytest.raises(ServicePrincipalAccessDeniedError),
            service_principal_context(
                principal_id=principal.pk, clinic_id=principal.clinic_id
            ),
        ):
            pytest.fail("stale-snapshot context accepted")
    finally:
        with agent.cursor() as cursor:
            cursor.execute("RESET default_transaction_isolation")


class SealedCatalog(Catalog):
    """Catalog closure that stops at the reviewed refusal helper.

    The helper is proven on its own (machine_violations); every other member is
    analysed as if the helper were an opaque, already-certified refusal.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sealed = {
            oid for oid, fn in self.functions.items() if fn.name == REFUSAL_HELPER
        }
        assert len(self.sealed) == 1, "refusal helper missing or overloaded"
        self.channels = self.derive()
        self.admitted = _admitted_gates(self)

    def _function(self, oid: int, *, bypass: bool, seen: set[Node]) -> Reads:
        if oid in self.sealed:
            return Reads(functions={oid})
        return super()._function(oid, bypass=bypass, seen=seen)


def function_digest(signature: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef(p.oid) || ' --owner ' || "
            "pg_get_userbyid(p.proowner) || ' --acl ' || p.proacl::text "
            "FROM pg_proc p WHERE p.oid = %s::regprocedure",
            [signature],
        )
        row = cursor.fetchone()
    assert row is not None
    return hashlib.sha256(str(row[0]).encode()).hexdigest()


def refusal_helper_digest() -> str:
    return function_digest(REFUSAL_HELPER + "(uuid,uuid)")


def _volatile_calls(catalog: Catalog, reads: Reads, exempt: Iterable[str]) -> list[str]:
    """Functions in a closure outside READ_ONLY_VOLATILITY, apart from exempt."""
    return sorted(
        function.name
        for function in (catalog.functions[oid] for oid in reads.functions)
        if (
            function.volatility not in READ_ONLY_VOLATILITY
            or function.name in XID_ASSIGNING
        )
        and function.name not in set(exempt)
    )


def _admitted_gates(catalog: SealedCatalog) -> frozenset[str]:
    """The largest subset of VOLATILE_GATES whose live definitions are the
    reviewed ones and whose own closures (the helper unsealed) write nothing,
    write no setting, hold nothing uninspectable and call no VOLATILE function
    outside the subset itself."""
    closures: dict[str, Reads] = {}
    sealed, catalog.sealed = catalog.sealed, set()
    try:
        with connection.cursor() as cursor:
            for name, (signature, digest) in VOLATILE_GATES.items():
                if function_digest(signature) != digest:
                    continue
                cursor.execute("SELECT %s::regprocedure::oid", [signature])
                row = cursor.fetchone()
                assert row is not None
                closures[name] = catalog._function(row[0], bypass=False, seen=set())
    finally:
        catalog.sealed = sealed
    admitted = set(closures)
    while rejected := {
        name
        for name in admitted
        if closures[name].writes
        or _setting_writes(catalog, closures[name])
        or closures[name].opaque - MACHINE_SQL_OPAQUE
        or _volatile_calls(catalog, closures[name], admitted)
    }:
        admitted -= rejected
    return frozenset(admitted)


def _views(catalog: SealedCatalog, reads: Reads) -> list[str]:
    return sorted(
        catalog.relations[oid].name
        for oid in reads.relations
        if catalog.relations[oid].kind in {"v", "m"}
    )


def _helper_violations(catalog: SealedCatalog, reads: Reads) -> list[str]:
    problems = []
    if refusal_helper_digest() != REFUSAL_HELPER_DIGEST:
        problems.append("refusal helper differs from the reviewed definition")
    relations = {catalog.relations[oid].name for oid in reads.relations}
    if relations != REFUSAL_HELPER_RELATIONS:
        problems.append("refusal helper reads: " + ", ".join(sorted(relations)))
    if reads.settings != REFUSAL_HELPER_SETTINGS:
        problems.append("refusal helper settings: " + ", ".join(sorted(reads.settings)))
    return problems


def _member_violations(catalog: SealedCatalog, reads: Reads) -> list[str]:
    problems = []
    if not reads.functions & catalog.sealed:
        problems.append(UNREACHED)
    if extra := reads.settings - MEMBER_SETTINGS:
        problems.append(
            "reads settings outside the allowlist: " + ", ".join(sorted(extra))
        )
    return problems


def _member_source(name: str) -> tuple[str, str, bool, list[str], list[str]]:
    """Language, body, single-boolean return, parameter names and types."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT l.lanname, p.prosrc, p.prorettype = 'boolean'::regtype"
            " AND NOT p.proretset AND p.proargmodes IS NULL,"
            " COALESCE(p.proargnames, ARRAY[]::text[]),"
            " ARRAY(SELECT format_type(t, NULL) FROM unnest(p.proargtypes) t)"
            " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
            " JOIN pg_language l ON l.oid = p.prolang"
            " WHERE n.nspname || '.' || p.proname = %s",
            [name],
        )
        ((language, source, returns_boolean, parameters, types),) = cursor.fetchall()
    return language, source, returns_boolean, list(parameters), list(types)


def _gate_violations(name: str) -> list[str]:
    """The member's result must be gated by principal_scope for every uuid
    parameter, first thing, with no setting writes (machine_gate)."""
    language, source, returns_boolean, parameters, types = _member_source(name)
    return member_gate(
        language=language,
        source=source,
        returns_boolean=returns_boolean,
        parameters=parameters,
        types=types,
    )


def _setting_writes(catalog: SealedCatalog, reads: Reads) -> list[str]:
    """Setting writes anywhere in a closure: set_config calls, SET/RESET in
    function bodies, and function-level SET configuration other than
    search_path."""
    writers: set[str] = set()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT oid, config FROM pg_proc, unnest(proconfig) config"
            " WHERE oid = ANY(%s) AND config NOT LIKE 'search\\_path=%%'",
            [sorted(reads.functions)],
        )
        writers.update(
            catalog.functions[oid].name for oid, _config in cursor.fetchall()
        )
    for oid in reads.functions:
        function = catalog.functions[oid]
        if function.name == "pg_catalog.set_config" or (
            function.language in {"sql", "plpgsql"} and setting_writes(function.source)
        ):
            writers.add(function.name)
    return sorted(writers)


def _closure_violations(catalog: SealedCatalog, reads: Reads) -> list[str]:
    problems = []
    if reads.relations & catalog.channels.relations:
        problems.append("reads a staff authority relation")
    if reads.functions & catalog.channels.functions:
        problems.append("calls a staff authority function")
    if reads.opaque - MACHINE_SQL_OPAQUE:
        problems.append("uninspectable read: " + ", ".join(sorted(reads.opaque)))
    if views := _views(catalog, reads):
        # View text is parsed by the catalog; the helper may read none at all.
        problems.append("reads views: " + ", ".join(views))
    if writers := _setting_writes(catalog, reads):
        problems.append(SETTING_WRITE + ": " + ", ".join(writers))
    # Cross-check derived from the catalog: no writing statement anywhere.
    if reads.writes:
        problems.append(WRITES_RELATION + ": " + ", ".join(sorted(reads.writes)))
    if volatile := _volatile_calls(catalog, reads, catalog.admitted):
        problems.append(VOLATILE_CALL + ": " + ", ".join(volatile))
    return problems


def machine_violations(
    catalog: SealedCatalog, members: list[str]
) -> dict[str, list[str]]:
    """Structural rule for every registry member, independent of actor values.

    The refusal helper is the reviewed definition pinned by digest, reading
    exactly its principal tables and settings and no view. Every other member
    must have its result gated by principal_scope on its parsed body
    (machine_gate), so no other input can widen a grant. As cross-checks, with
    the helper sealed, it must reach the helper and read no setting outside
    MEMBER_SETTINGS, no view, and no staff authority channel, directly or
    through defaults, policies, triggers or helper functions. Uninspectable
    reads, including settings catalogs, fail closed.
    """
    assert REFUSAL_HELPER in members, "refusal helper is not a registry member"
    result: dict[str, list[str]] = {}
    for name, types in _argument_types(members).items():
        statement = f"SELECT {name}({', '.join(f'NULL::{t}' for t in types)})"
        if name == REFUSAL_HELPER:
            sealed, catalog.sealed = catalog.sealed, set()
            try:
                reads = catalog.statement(statement)
            finally:
                catalog.sealed = sealed
            problems = _helper_violations(catalog, reads)
        else:
            reads = catalog.statement(statement)
            problems = _member_violations(catalog, reads) + _gate_violations(name)
        result[name] = problems + _closure_violations(catalog, reads)
    return result


def test_machine_sql_closure_reads_no_staff_authority() -> None:
    members = machine_members()
    assert members
    assert machine_violations(SealedCatalog(), members) == {m: [] for m in members}


# Planted views: an actor reader and a settings enumerator.
PLANTED_VIEWS = """
    CREATE VIEW clinic_app.principal_actor_v AS
      SELECT current_setting('app.current_user_id', true) AS actor;
    CREATE VIEW clinic_app.principal_settings_v AS
      SELECT name, setting FROM pg_catalog.pg_settings;
    CREATE FUNCTION clinic_app.principal_actor_hint() RETURNS text
      LANGUAGE sql STABLE SET search_path=pg_catalog,clinic_app,pg_temp
      AS $f$ SELECT current_setting('app.current_user_id', true) $f$;
    CREATE FUNCTION clinic_app.principal_default_hint(
      actor text DEFAULT current_setting('app.current_user_id', true))
      RETURNS text LANGUAGE sql STABLE
      SET search_path=pg_catalog,clinic_app,pg_temp AS $f$ SELECT actor $f$;
    CREATE FUNCTION clinic_app.principal_reset_hint() RETURNS text
      LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,clinic_app,pg_temp
      AS $f$ BEGIN RESET app.current_user_id; RETURN ''; END $f$;
    CREATE FUNCTION clinic_app.principal_write_hint(c uuid) RETURNS boolean
      LANGUAGE sql VOLATILE SET search_path=pg_catalog,clinic_app,pg_temp
      AS $f$ INSERT INTO clinic_app.prescription_verificationprobe
        (probe_key, window_start, lookups)
        VALUES (decode(md5(c::text), 'hex'), now(), 1) RETURNING true $f$;
    CREATE FUNCTION clinic_app.principal_delete_hint() RETURNS boolean
      LANGUAGE plpgsql STABLE SET search_path=pg_catalog,clinic_app,pg_temp
      AS $f$ BEGIN DELETE FROM clinic_app.prescription_verificationprobe
        WHERE false; RETURN true; END $f$;
"""
MACHINE_PRINCIPAL = "NULLIF(current_setting('app.current_principal',true),'')::uuid"
MACHINE_GATE = f"clinic_app.principal_scope({MACHINE_PRINCIPAL}, clinic)"
PLANTED_MEMBERS = {
    # Grants when the actor names a practitioner (review round 3, MY2).
    "practitioner_join": """
        SELECT CASE WHEN NULLIF(current_setting('app.current_user_id',true),'')
          IS NULL THEN clinic::text = current_setting('app.current_principal',true)
        ELSE EXISTS (SELECT 1 FROM clinic_app.scheduling_availabilityblock b
          WHERE b.practitioner_id::text = current_setting('app.current_user_id',true))
        END AND pg_has_role(session_user,'clinic_agent','USAGE')""",
    # Grants when the actor GUC is set but is not a uuid (review round 3, MY1).
    "non_uuid": """
        SELECT CASE WHEN NULLIF(current_setting('app.current_user_id',true),'')
          IS NULL THEN clinic::text = current_setting('app.current_principal',true)
        ELSE current_setting('app.current_user_id',true) !~ '^[0-9a-f-]{36}$'
        END AND pg_has_role(session_user,'clinic_agent','USAGE')""",
    # Reaches the helper, but reads the actor through a new helper function.
    "actor_helper": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR clinic_app.principal_actor_hint() = clinic::text""",
    # Reaches the helper, but enumerates settings.
    "settings_catalog": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR EXISTS (SELECT 1 FROM pg_catalog.pg_settings
            WHERE name = 'app.current_user_id' AND setting = clinic::text)""",
    # Actor read through a view, joined to practitioners (review round 4, MZ6).
    "view_practitioner": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.principal_actor_v v
            JOIN clinic_app.scheduling_availabilityblock b
              ON b.practitioner_id::text = v.actor)""",
    # The patient actor setting (review round 4, MZ1).
    "patient_session": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR current_setting('app.current_patient_session', true) = clinic::text""",
    # An invented caller-set setting (review round 4, MZ2).
    "on_behalf_of": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR current_setting('app.on_behalf_of', true) = clinic::text""",
    # Settings enumerated through a view.
    "settings_view": """
        SELECT clinic_app.principal_scope(clinic, clinic) IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.principal_settings_v s
            WHERE s.name = 'app.current_user_id' AND s.setting = clinic::text)""",
    # The allowlisted, caller-settable principal GUC used as an identity, OR-ed
    # around the gate (review round 5, A7).
    "or_principal": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.scheduling_availabilityblock b
            WHERE b.practitioner_id::text
              = current_setting('app.current_principal', true))""",  # noqa: S608 - fixed plant SQL.
    # Identity supplied as an argument, OR-ed around the gate (round 5, A2).
    "or_argument": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
            WHERE a.practitioner_id = clinic)""",  # noqa: S608 - fixed plant SQL.
    # The actor through a helper's parameter DEFAULT, OR-ed (round 5, A6b).
    "default_or": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
            WHERE a.practitioner_id::text = clinic_app.principal_default_hint())""",  # noqa: S608 - fixed plant SQL.
    # Gated, but a DEFAULT still reads the actor: the allowlist sees it.
    "default_gated": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          AND clinic_app.principal_default_hint() = clinic::text""",
    # Positive control: a gated SQL member is accepted.
    "sql_gated": f"SELECT {MACHINE_GATE} IS NOT NULL AND clinic IS NOT NULL",
    "two_uuid": f"SELECT {MACHINE_GATE} IS NOT NULL AND target_clinic IS NOT NULL",
    # The gate checks clinic; the member decides for an array of clinics.
    "uuid_array": f"SELECT {MACHINE_GATE} IS NOT NULL AND targets IS NOT NULL",
    # Function-level configuration other than search_path changes what the
    # whole call, gate included, observes. (A header SET of an app.* setting
    # needs a superuser or a GRANT SET ON PARAMETER; PostgreSQL refuses it to
    # clinic_resolver.)
    "header_set": f"SELECT {MACHINE_GATE} IS NOT NULL AND clinic IS NOT NULL",
    # Gated, but a helper resets the actor for later calls in the transaction.
    "helper_reset": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          AND clinic_app.principal_reset_hint() = ''""",
    # A helper conjunct writes whether or not the gate refuses (review round 8,
    # D1-r8): PostgreSQL does not order AND conjuncts.
    "write_helper": f"""
        SELECT clinic_app.principal_write_hint(clinic)
          AND {MACHINE_GATE} IS NOT NULL""",
    # A STABLE-declared helper whose body writes: the catalog still sees it.
    "stable_delete_helper": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          AND clinic_app.principal_delete_hint()""",
    # Volatile side effects before or beside the gate.
    "advisory_lock": f"""
        SELECT (SELECT pg_advisory_xact_lock(7)) IS NULL
          AND {MACHINE_GATE} IS NOT NULL""",
    "notify": f"""
        SELECT {MACHINE_GATE} IS NOT NULL
          AND pg_notify('sintetico', clinic::text) IS NULL""",
    # A STABLE builtin that still assigns a transaction id.
    "xid_assign": f"SELECT {MACHINE_GATE} IS NOT NULL AND txid_current() > 0",
    # A gated member declared VOLATILE: PostgreSQL would let its body write.
    "volatile_member": f"SELECT {MACHINE_GATE} IS NOT NULL AND clinic IS NOT NULL",
    # Positive control: each uuid parameter behind its own gate.
    "two_uuid_gated": (
        f"SELECT {MACHINE_GATE} IS NOT NULL AND clinic_app.principal_scope("
        f"{MACHINE_PRINCIPAL}, target_clinic) IS NOT NULL"
    ),
    # Gated on the principal's own clinic, ignoring the clinic asked about
    # (review round 6, G1): grants any clinic, in any organization.
    "own_clinic_gate": f"""
        SELECT clinic_app.principal_scope({MACHINE_PRINCIPAL},
          (SELECT s.clinic_id FROM clinic_app.identity_serviceprincipal s
            WHERE s.id = {MACHINE_PRINCIPAL})) IS NOT NULL""",  # noqa: S608 - fixed plant SQL.
}
PLPGSQL_EARLY = """
    IF current_setting('app.current_principal', true) IN (
      SELECT b.practitioner_id::text FROM clinic_app.scheduling_availabilityblock b)
    THEN RETURN true; END IF;"""
PLPGSQL_MEMBER = """
    DECLARE registered uuid;
    BEGIN{early}
     registered := {gate};
     IF registered IS NULL THEN RETURN false; END IF;
     RETURN true;
    END"""
PLANTED_PLPGSQL_MEMBERS = {
    # An early RETURN true before the gate (review round 5).
    "plpgsql_early_return": PLPGSQL_MEMBER.format(
        early=PLPGSQL_EARLY, gate=MACHINE_GATE
    ),
    # Positive control: the same member without the early return.
    "plpgsql_gated": PLPGSQL_MEMBER.format(early="", gate=MACHINE_GATE),
    # The clinic parameter is rebound to the principal's own clinic before
    # the gate (review round 6).
    "plpgsql_reassigned_clinic": PLPGSQL_MEMBER.format(
        early=f"""
     clinic := (SELECT s.clinic_id FROM clinic_app.identity_serviceprincipal s
       WHERE s.id = {MACHINE_PRINCIPAL});""",  # noqa: S608 - fixed plant SQL.
        gate=MACHINE_GATE,
    ),
    # A data-conditional SET LOCAL clearing the actor before the gate
    # (review round 7, T4b).
    "plpgsql_set_local": PLPGSQL_MEMBER.format(
        early="""
     IF (SELECT count(*) FROM clinic_app.scheduling_appointment) > 0
     THEN SET LOCAL app.current_user_id = ''; END IF;""",
        gate=MACHINE_GATE,
    ),
    # A DECLARE initialiser that clears the actor before the gate.
    "plpgsql_declare_set_config": PLPGSQL_MEMBER.replace(
        "DECLARE registered uuid;",
        "DECLARE registered uuid;"
        " cleared text := set_config('app.current_user_id', '', true);",
    ).format(early="", gate=MACHINE_GATE),
    # A RESET after the gate, which later calls in the transaction observe.
    "plpgsql_reset_after": PLPGSQL_MEMBER.replace(
        "RETURN true;", "RESET app.current_user_id; RETURN true;"
    ).format(early="", gate=MACHINE_GATE),
}
# Plants with their own signature (default: clinic uuid).
PLANT_SIGNATURES = {
    # The gate checks clinic; the member decides for target_clinic (round 7).
    "two_uuid": "clinic uuid, target_clinic uuid",
    "two_uuid_gated": "clinic uuid, target_clinic uuid",
    "uuid_array": "clinic uuid, targets uuid[]",
}
# Plants with extra function-level configuration (default: search_path only).
PLANT_HEADERS = {"header_set": " SET TimeZone = 'UTC'"}
# Plants are STABLE unless the plant is about volatility.
PLANT_VOLATILITY = {"volatile_member": "VOLATILE"}
UNREACHED = "does not reach the refusal helper"
SETTINGS = "reads settings outside the allowlist"
OPAQUE = "uninspectable read"
VIEWS = "reads views"
PLANT_VIOLATIONS = {
    "practitioner_join": {UNREACHED, SETTINGS, NOT_GATED},
    "non_uuid": {UNREACHED, SETTINGS, NOT_GATED},
    "actor_helper": {SETTINGS, NOT_GATED},
    "settings_catalog": {OPAQUE, NOT_GATED},
    "view_practitioner": {SETTINGS, VIEWS, NOT_GATED},
    "patient_session": {SETTINGS, NOT_GATED},
    "on_behalf_of": {SETTINGS, NOT_GATED},
    "settings_view": {OPAQUE, VIEWS, NOT_GATED},
    "or_principal": {NOT_GATED},
    "or_argument": {NOT_GATED},
    "default_or": {SETTINGS, NOT_GATED},
    "default_gated": {SETTINGS},
    "sql_gated": set(),
    "plpgsql_early_return": {NOT_GATED},
    "plpgsql_gated": set(),
    "own_clinic_gate": {UNBOUND_CLINIC},
    "plpgsql_reassigned_clinic": {NOT_GATED},
    "plpgsql_set_local": {NOT_GATED, SETTING_WRITE, SETTINGS},
    "plpgsql_declare_set_config": {NOT_GATED, SETTING_WRITE, SETTINGS, VOLATILE_CALL},
    "plpgsql_reset_after": {SETTING_WRITE, SETTINGS},
    "two_uuid": {UNGATED_UUID},
    "uuid_array": {UNGATED_UUID},
    "header_set": {SETTING_WRITE},
    "write_helper": {WRITES_RELATION, VOLATILE_CALL},
    "stable_delete_helper": {WRITES_RELATION},
    "advisory_lock": {VOLATILE_CALL},
    "notify": {OPAQUE, VOLATILE_CALL},
    "volatile_member": {VOLATILE_CALL},
    "xid_assign": {VOLATILE_CALL},
    "two_uuid_gated": set(),
    "helper_reset": {SETTING_WRITE, SETTINGS, VOLATILE_CALL},
}


def _kinds(problems: list[str]) -> set[str]:
    return {problem.split(":", 1)[0] for problem in problems}


@pytest.mark.parametrize("plant", sorted(PLANTED_MEMBERS | PLANTED_PLPGSQL_MEMBERS))
def test_planted_actor_reader_is_refused_by_construction(plant: str) -> None:
    language = "plpgsql" if plant in PLANTED_PLPGSQL_MEMBERS else "sql"
    body = (PLANTED_MEMBERS | PLANTED_PLPGSQL_MEMBERS)[plant]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(PLANTED_VIEWS)
        signature = PLANT_SIGNATURES.get(plant, "clinic uuid")
        cursor.execute(
            f"CREATE FUNCTION clinic_app.principal_extra({signature}) RETURNS boolean"
            f" LANGUAGE {language} {PLANT_VOLATILITY.get(plant, 'STABLE')}"
            " SECURITY DEFINER"
            " SET search_path=pg_catalog,clinic_app,pg_temp"
            f"{PLANT_HEADERS.get(plant, '')} AS $f$ {body} $f$"
        )
        violations = machine_violations(
            SealedCatalog(), [*machine_members(), "clinic_app.principal_extra"]
        )
        transaction.set_rollback(True)
    assert (
        _kinds(violations.pop("clinic_app.principal_extra"))
        == (PLANT_VIOLATIONS[plant])
    ), plant
    assert violations == {member: [] for member in machine_members()}


WEAKENED_HELPERS = {
    # The refusal conjunct is OR-ed away.
    "or": "OR NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL",
    # The conjunct is dropped.
    "dropped": "",
    # Review round 4 (W3): the conjunct is kept but inert, and a view admits
    # any actor with outbox history.
    "inert_view": """
        AND (SELECT count(*) WHERE true
          AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL) >= 0
        AND (SELECT count(*) FROM clinic_app.principal_actor_v v
          WHERE NULLIF(v.actor,'') IS NULL)
          + (SELECT count(*) FROM clinic_app.principal_actor_v v
            JOIN clinic_app.comms_integrationoperation o
              ON o.actor_id::text = v.actor) > 0""",
}


@pytest.mark.parametrize("helper", sorted(WEAKENED_HELPERS))
def test_weakened_refusal_helper_is_refused(helper: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.principal_scope(uuid,uuid)'"
            "::regprocedure)"
        )
        row = cursor.fetchone()
        assert row is not None
        weakened = str(row[0]).replace(
            "AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL",
            WEAKENED_HELPERS[helper],
        )
        assert weakened != row[0]
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(PLANTED_VIEWS)
        cursor.execute(weakened)
        violations = machine_violations(SealedCatalog(), machine_members())
        transaction.set_rollback(True)
    assert (
        "refusal helper differs from the reviewed definition"
        in (violations[REFUSAL_HELPER])
    ), helper


def test_refusal_helper_pin_is_the_live_definition() -> None:
    assert refusal_helper_digest() == REFUSAL_HELPER_DIGEST


def test_volatile_gate_exception_is_exact() -> None:
    """The VOLATILE exception is exactly the refusal helper plus every function
    a clinic_agent RLS policy calls (they need a fresh snapshot per row), and
    every one of them is admitted on the live catalog."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid)"
            " FROM pg_policy WHERE 'clinic_agent'::regrole = ANY(polroles)"
        )
        expressions = [text for row in cursor.fetchall() for text in row if text]
    assert expressions, "no clinic_agent policy"
    catalog = SealedCatalog()
    calls = {call for text in expressions for call in references(text).calls}
    # Resolved as the census resolves names; an unresolved call fails closed.
    assert all(catalog.function_names.get(call) for call in calls), calls
    policy_calls = {
        catalog.functions[oid].name
        for call in calls
        for oid in catalog.function_names[call]
    }
    assert set(VOLATILE_GATES) == {REFUSAL_HELPER} | policy_calls
    assert catalog.admitted == set(VOLATILE_GATES)


# Each shape takes the admission away from the named gates while leaving their
# volatility as it is: statements to run, and a query returning one more
# statement to run (or None).
UNPROVEN_GATES: dict[str, tuple[list[str], str | None, set[str]]] = {
    # A reviewed-looking change invisible to the write rule: a row lock in a
    # VOLATILE body. Only the definition pin sees it.
    "for_share_body": (
        [],
        """
        SELECT replace(pg_get_functiondef(
          'clinic_app.principal_has(text,uuid)'::regprocedure),
          ' RETURN EXISTS', ' PERFORM 1 FROM clinic_app.identity_serviceprincipal'
          || ' FOR SHARE; RETURN EXISTS')""",
        {"clinic_app.principal_has"},
    ),
    # The definitions are untouched, but their closures now reach a VOLATILE
    # function through the = operator both bodies use.
    "volatile_operator": (
        [
            """
            CREATE FUNCTION clinic_app.principal_eq_hint(a uuid, b text)
              RETURNS boolean LANGUAGE sql VOLATILE
              SET search_path=pg_catalog,clinic_app,pg_temp
              AS $f$ SELECT a::text = b $f$""",
            """
            CREATE OPERATOR clinic_app.= (LEFTARG = uuid, RIGHTARG = text,
              FUNCTION = clinic_app.principal_eq_hint)""",
        ],
        None,
        {"clinic_app.principal_has", REFUSAL_HELPER},
    ),
}


@pytest.mark.parametrize("shape", sorted(UNPROVEN_GATES))
def test_unproven_gate_loses_its_volatile_exception(shape: str) -> None:
    statements, rewrite, unproven = UNPROVEN_GATES[shape]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        for statement in statements:
            cursor.execute(statement)
        if rewrite:
            cursor.execute(rewrite)
            row = cursor.fetchone()
            assert row is not None
            assert "FOR SHARE" in row[0]
            cursor.execute(row[0])
        catalog = SealedCatalog()
        violations = machine_violations(catalog, machine_members())
        transaction.set_rollback(True)
    assert catalog.admitted == set(VOLATILE_GATES) - unproven
    for name in unproven:
        # The gate itself is now an unadmitted VOLATILE function.
        calls = [v for v in violations[name] if v.startswith(VOLATILE_CALL + ":")]
        assert calls, (name, violations[name])
        assert name in calls[0].split(": ", 1)[1].split(", "), calls


def test_forged_actor_with_domain_history_is_refused_on_the_agent_login(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real staff actor with availability and outbox history is still refused."""
    monkeypatch.setattr(execute_operation_task, "apply_async", lambda **_kwargs: None)
    actor = rbac_graph.physician
    assert _seed_availability(rbac_graph)
    with runtime_role(), tenant_context(actor, rbac_graph.organization_a):
        enqueue_operation(
            OperationRequest(
                channel="email",
                provider="synthetic",
                clinic_id=principal.clinic_id,
                subject_type="synthetic.action",
                subject_id=uuid4(),
                idempotency_key=uuid4(),
            )
        )
    members = machine_members()
    types = _argument_types(members)
    vectors = _machine_vectors(
        types,
        {
            "uuid": [principal.pk, principal.clinic_id, actor, uuid4()],
            "text": ["appointment.read"],
        },
    )
    calls = {name: (_call(name, types[name]), vectors[name]) for name in members}
    # The staff actor with history, plus non-uuid values: any non-empty GUC.
    forged: list[object] = [actor, "x", " "]
    decisions = _machine_decisions(
        principal,
        {"cleared": [None], "forged": forged},
        {"own": principal.organization_id},
        calls,
    )
    assert {name for name, _t, _a in decisions} == set(members)
    for name in members:
        # Anti-vacuity: the same vectors grant with the actor GUC cleared.
        assert any(map(_granted, decisions[name, "own", "cleared"])), name
        assert len(decisions[name, "own", "forged"]) == len(forged) * len(
            vectors[name]
        ), name
        # The forged actors, and the actor equal to each argument, never grant.
        for cell in ("forged", "own_argument"):
            assert decisions[name, "own", cell], (name, cell)
            assert not any(map(_granted, decisions[name, "own", cell])), (name, cell)
    agent = connections["agent"]
    with agent.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, false)", [str(actor)]
        )
    try:
        with (
            pytest.raises(ServicePrincipalAccessDeniedError),
            service_principal_context(
                principal_id=principal.pk, clinic_id=principal.clinic_id
            ),
        ):
            pytest.fail("forged staff actor entered the machine context")
    finally:
        with agent.cursor() as cursor:
            cursor.execute("RESET app.current_user_id")


def test_planted_staff_read_in_machine_sql_is_detected() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute("""
            CREATE OR REPLACE FUNCTION clinic_app.principal_has(perm text, clinic uuid)
            RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
            SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
             SELECT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
              WHERE r.clinic_id=clinic AND r.role='owner')
            $f$
        """)
        violations = machine_violations(SealedCatalog(), machine_members())
        transaction.set_rollback(True)
    assert "reads a staff authority relation" in violations["clinic_app.principal_has"]
    assert UNREACHED in violations["clinic_app.principal_has"]


def test_machine_matrix_fails_closed_without_executable_members() -> None:
    member = "clinic_app.synthetic_member"
    with pytest.raises(AssertionError, match="no argument pool"):
        _machine_vectors({member: ["integer"]}, {"uuid": [uuid4()]})
    with pytest.raises(AssertionError, match="not executed"):
        _assert_every_member_executed([member], {})
    empty = {(member, t, a): () for t in ("own", "foreign") for a in ACTOR_GUCS}
    with pytest.raises(AssertionError, match="no executed decisions"):
        _assert_every_member_executed([member], empty)
    assert machine_members({"sql_guards": {}}) == []


def _machine_decisions(
    principal: ServicePrincipal,
    actors: dict[str, list[object]],
    tenants: dict[str, UUID],
    calls: dict[str, tuple[str, list[tuple[UUID | str, ...]]]],
) -> dict[tuple[str, str, str], tuple[object, ...]]:
    """Run every member on the agent login under each tenant and actor GUC.

    Fixed cells run every vector under each listed actor value. The
    own_argument cell sets the actor GUC to each argument of the vector it
    then calls, so actor == argument is exercised for every argument.
    """
    decisions = {}
    with transaction.atomic(using="agent"), connections["agent"].cursor() as cursor:

        def decide(
            statement: str, vector: tuple[UUID | str, ...], actor: object
        ) -> object:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(actor or "")],
            )
            cursor.execute(statement, list(vector))
            row = cursor.fetchone()
            assert row is not None
            return row[0]

        for tenant_label, tenant in tenants.items():
            cursor.execute(
                "SELECT set_config('app.current_principal',%s,true), "
                "set_config('app.current_tenant',%s,true)",
                [str(principal.pk), str(tenant)],
            )
            for name, (statement, vectors) in calls.items():
                for actor_label, values in actors.items():
                    decisions[name, tenant_label, actor_label] = tuple(
                        decide(statement, vector, actor)
                        for actor in values
                        for vector in vectors
                    )
                decisions[name, tenant_label, "own_argument"] = tuple(
                    decide(statement, vector, argument)
                    for vector in vectors
                    for argument in vector
                )
        transaction.set_rollback(True, using="agent")
    return decisions


def _runtime_refusals(
    types: dict[str, list[str]], actor: UUID, organization: UUID
) -> dict[str, object]:
    refusals: dict[str, object] = {}
    for name, argument_types in types.items():
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true), "
                "set_config('app.current_tenant',%s,true)",
                [str(actor), str(organization)],
            )
            with pytest.raises(DatabaseError) as error:
                cursor.execute(
                    _call(name, argument_types), [None] * len(argument_types)
                )
            transaction.set_rollback(True)
        refusals[name] = type(error.value.__cause__)
    return refusals


def test_every_uuid_argument_refuses_foreign_clinics_on_the_agent_login(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    """Actor cleared: each uuid argument independently takes the own, a
    same-org and an other-org clinic, under both tenants; any grant with a
    foreign clinic argument fails. Text arguments also carry the foreign
    clinic ids, so a clinic smuggled as text is refused too."""
    members = machine_members()
    types = _argument_types(members)
    foreign = [rbac_graph.clinic_b, rbac_graph.clinic_c]
    pools: dict[str, list[UUID | str]] = {
        "uuid": [principal.clinic_id, *foreign],
        "text": ["appointment.read", "clinical.finalize", *map(str, foreign)],
    }
    foreign_values: set[object] = {*foreign, *map(str, foreign)}
    calls = {}
    for name in members:
        if name == REFUSAL_HELPER:
            # The pinned helper's first parameter is the principal, not a clinic.
            assert _member_source(name)[3] == [
                "requested_principal",
                "requested_clinic",
            ]
            vectors: list[tuple[UUID | str, ...]] = [
                (principal.pk, clinic) for clinic in pools["uuid"]
            ]
        else:
            vectors = _machine_vectors({name: types[name]}, pools)[name]
        calls[name] = (_call(name, types[name]), vectors)
    tenants = {"own": principal.organization_id, "foreign": rbac_graph.organization_b}
    decisions = _machine_decisions(principal, {"cleared": [None]}, tenants, calls)
    for name, (_statement, vectors) in calls.items():
        for tenant in tenants:
            values = decisions[name, tenant, "cleared"]
            assert len(values) == len(vectors), (name, tenant)
            for vector, value in zip(vectors, values, strict=True):
                if foreign_values.intersection(vector):
                    assert not _granted(value), (name, tenant, vector)
        # Anti-vacuity: an all-own vector grants under the own tenant.
        assert any(
            _granted(value)
            for vector, value in zip(
                vectors, decisions[name, "own", "cleared"], strict=True
            )
            if not foreign_values.intersection(vector)
        ), (name, "never grants")


def _database_state(admin: psycopg.Connection[Any]) -> dict[str, object]:
    """Every row version (ctid, xmin, xmax: inserts, updates, deletes and row
    locks) of every table, matview and large object, and every sequence
    position, read as the superuser past RLS."""
    relations = admin.execute(
        "SELECT c.oid::regclass::text, c.relkind FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relkind IN ('r', 'm', 'S')"
        " AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
        " AND n.nspname !~ '^pg_(toast|temp_)'"
    ).fetchall()
    rows = sql.SQL(
        "SELECT count(*), md5(string_agg(concat_ws(':', t.ctid, t.xmin, t.xmax),"
        " ',' ORDER BY t.ctid)) FROM {} t"
    )
    sequence = sql.SQL("SELECT last_value, is_called FROM {}")
    state: dict[str, object] = {}
    for name, kind in [*relations, ("pg_catalog.pg_largeobject_metadata", "r")]:
        query = sequence if kind == "S" else rows
        # Names come from pg_class as regclass text, already quoted.
        state[name] = admin.execute(query.format(sql.SQL(name))).fetchone()
    assert len(state) > len(relations), "empty catalog snapshot"
    return state


def test_machine_calls_leave_every_table_and_sequence_unchanged(
    principal: ServicePrincipal, rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    """Review round 8 (D1-r8): every member runs on the real agent login
    under refusing states (forged actor, foreign clinics, foreign tenant) and
    granting ones, each call committed on its own, and the whole database is
    unchanged afterwards.

    Boundary: this compares transactional table and sequence state only (row
    versions of every table and matview, pg_largeobject_metadata, sequence
    positions). It does not see session advisory locks, queued NOTIFY,
    session GUCs, large-object data pages, temp tables or xid consumption.
    Those are refused structurally: every closure function must be
    non-volatile (pg_advisory*, pg_notify, lo_*, set_config are VOLATILE;
    txid_current and pg_current_xact_id are excluded by name), setting writes
    and writing statements are refused, and the two VOLATILE gates are
    admitted only with their reviewed definitions (VOLATILE_GATES)."""
    members = machine_members()
    types = _argument_types(members)
    pools: dict[str, list[UUID | str]] = {
        "uuid": [principal.clinic_id, rbac_graph.clinic_b, rbac_graph.clinic_c],
        "text": ["appointment.read", str(rbac_graph.clinic_c)],
    }
    calls = {
        name: (
            [(principal.pk, clinic) for clinic in pools["uuid"]]
            if name == REFUSAL_HELPER
            else _machine_vectors({name: types[name]}, pools)[name]
        )
        for name in members
    }
    tenants = [principal.organization_id, rbac_graph.organization_b]
    actors = ["", str(rbac_graph.physician)]
    outcomes: dict[str, set[bool]] = {name: set() for name in members}
    params = connections["agent"].get_connection_params()
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:
        before = _database_state(admin)
        with psycopg.connect(**params, autocommit=True) as agent:
            for tenant, actor in product(tenants, actors):
                agent.execute(
                    "SELECT set_config('app.current_principal', %s, false),"
                    " set_config('app.current_tenant', %s, false),"
                    " set_config('app.current_user_id', %s, false)",
                    [str(principal.pk), str(tenant), actor],
                )
                for name, vectors in calls.items():
                    for vector in vectors:
                        row = agent.execute(
                            _call(name, types[name]), list(vector)
                        ).fetchone()
                        assert row is not None
                        outcomes[name].add(_granted(row[0]))
        after = _database_state(admin)
    # Both paths ran for every member: refusals, and grants as anti-vacuity.
    assert outcomes == {name: {False, True} for name in members}
    changed = sorted(name for name in before if before[name] != after.get(name))
    assert not changed, changed
    assert after == before


def test_staff_state_never_grants_machine_authority(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    members = machine_members()
    types = _argument_types(members)
    uuid_pool: list[UUID | str] = [
        principal.pk,
        principal.clinic_id,
        principal.organization_id,
        rbac_graph.clinic_b,
        uuid4(),
    ]
    vectors = _machine_vectors(
        types, {"uuid": uuid_pool, "text": ["appointment.read", "clinical.finalize"]}
    )
    calls = {name: (_call(name, types[name]), vectors[name]) for name in members}
    actor = User.objects.create(username="synthetic-machine-staff-" + uuid4().hex)
    actors: dict[str, list[object]] = {
        "cleared": [None],
        "forged": [actor.pk],
        "mismatched": [rbac_graph.physician],
        "pooled": list(uuid_pool),
        # Non-uuid actor values: the refusal must not depend on the value's shape.
        "malformed": ["x", " ", "SINTETICO-not-a-uuid"],
    }
    assert (*actors, "own_argument") == ACTOR_GUCS
    tenants = {"own": principal.organization_id, "foreign": rbac_graph.organization_b}
    scope = ReplayScope(
        rbac_graph.clinic_a, rbac_graph.organization_a, other_clinic=rbac_graph.clinic_b
    )
    states = list(backstop_states(scope))
    observed = {}
    try:
        for state in states:
            User.objects.filter(pk=actor.pk).update(
                is_active=state.authority != "inactive"
            )
            set_memberships(actor, scope, state)
            add_authority_backstop(actor, scope, state)
            decisions = _machine_decisions(principal, actors, tenants, calls)
            _assert_every_member_executed(members, decisions)
            observed[state.key()] = (
                decisions,
                _runtime_refusals(types, actor.pk, principal.organization_id),
            )
    finally:
        set_memberships(actor, scope, StaffState(()))
    assert len(observed) == len(states)
    first, refusals = observed[states[0].key()]
    # Staff state never changes any machine decision or runtime refusal.
    assert all(value == (first, refusals) for value in observed.values())
    assert refusals == dict.fromkeys(members, psycopg.errors.InsufficientPrivilege)
    for (name, _tenant, actor_guc), values in first.items():
        if actor_guc == "cleared":
            continue
        assert not any(map(_granted, values)), (name, actor_guc, "actor GUC granted")
    for name in members:
        # Anti-vacuity: the pooled arguments reach each member's grant path.
        assert any(map(_granted, first[name, "own", "cleared"])), (name, "never grants")
        # Every argument was also presented as the actor GUC.
        assert len(first[name, "own", "own_argument"]) == len(types[name]) * len(
            vectors[name]
        ), (name, "actor == argument not exercised")

"""Owner-provisioned principals retain immutable, audited authority history."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.identity import service_principals
from apps.identity.current_context import CurrentActorError
from apps.identity.models import (
    ServicePrincipal,
    ServicePrincipalGrant,
    User,
    UserClinicRole,
)
from apps.identity.permissions import BUNDLES_V1
from apps.identity.service_principals import (
    grant_principal,
    register_principal,
    revoke_principal,
    revoke_principal_grant,
)
from django.db import ProgrammingError, connection, transaction

from identity.permission_support import owner_context, permission_actor
from identity.test_scope_provisioning import provisioning_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def principal_pair(rbac_graph: RbacGraph) -> tuple[ServicePrincipal, ServicePrincipal]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT session_user, current_user, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname=current_user"
        )
        assert cursor.fetchone() == ("clinic_owner", "clinic_owner", False, False)
    principals = []
    for organization_id, clinic_id, suffix in (
        (rbac_graph.organization_a, rbac_graph.clinic_a, "a"),
        (rbac_graph.organization_b, rbac_graph.clinic_c, "b"),
    ):
        with owner_context(organization_id):
            principal = ServicePrincipal.objects.create(
                organization_id=organization_id,
                clinic_id=clinic_id,
                name=f"sintetico-policy-{suffix}",
                db_identity=f"clinic_agent_{suffix}",
                purpose="availability",
            )
            ServicePrincipalGrant.objects.create(
                organization_id=organization_id,
                principal=principal,
                permission="appointment.read",
                subject_scope="clinic",
            )
            principals.append(principal)
    return principals[0], principals[1]


@pytest.mark.parametrize("model", [ServicePrincipal, ServicePrincipalGrant])
def test_owner_authority_reads_require_the_matching_tenant(
    model: type[ServicePrincipal | ServicePrincipalGrant],
    principal_pair: tuple[ServicePrincipal, ServicePrincipal],
) -> None:
    for principal in principal_pair:
        with owner_context(principal.organization_id):
            assert list(model.objects.values_list("organization_id", flat=True)) == [
                principal.organization_id
            ]
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_tenant','',true)")
        assert not model.objects.exists()


@pytest.mark.parametrize("model", [ServicePrincipal, ServicePrincipalGrant])
@pytest.mark.parametrize("context", ["foreign", "missing"])
def test_owner_authority_inserts_require_the_matching_tenant(
    model: type[ServicePrincipal | ServicePrincipalGrant],
    context: str,
    principal_pair: tuple[ServicePrincipal, ServicePrincipal],
) -> None:
    local, foreign = principal_pair
    row: ServicePrincipal | ServicePrincipalGrant
    if model is ServicePrincipal:
        row = ServicePrincipal(
            organization_id=foreign.organization_id,
            clinic_id=foreign.clinic_id,
            name="sintetico-policy-write",
            db_identity=f"clinic_agent_{uuid4().hex}",
            purpose="availability",
        )
    else:
        # Avoid the active-grant uniqueness constraint: only the policy should
        # reject this otherwise valid organization/principal pair.
        row = ServicePrincipalGrant(
            organization_id=foreign.organization_id,
            principal=foreign,
            permission="appointment.read",
            subject_scope="clinic",
            active=False,
        )
    with owner_context(foreign.organization_id):
        row.save(force_insert=True)
        assert model.objects.filter(pk=row.pk).exists()
    row.pk = uuid4()
    if isinstance(row, ServicePrincipal):
        row.db_identity = f"clinic_agent_{uuid4().hex}"
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)",
            [str(local.organization_id) if context == "foreign" else ""],
        )
        with pytest.raises(ProgrammingError) as error, transaction.atomic():
            row.save(force_insert=True)
        assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)
        assert error.value.__cause__.sqlstate == "42501"


def test_owner_registration_grant_and_revocations_are_audited_once(
    rbac_graph: RbacGraph,
) -> None:
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            role="owner",
        )
    with provisioning_context(rbac_graph, rbac_graph.shared_user):
        principal = register_principal(
            clinic_id=rbac_graph.clinic_a,
            name="sintetico-reader",
            db_identity="clinic_agent",
            purpose="availability",
        )
        grant = grant_principal(
            clinic_id=rbac_graph.clinic_a,
            principal_id=principal.pk,
            permission="appointment.read",
            subject_scope="clinic",
        )
        revoke_principal_grant(clinic_id=rbac_graph.clinic_a, grant_id=grant.pk)
        revoke_principal_grant(clinic_id=rbac_graph.clinic_a, grant_id=grant.pk)
        revoke_principal(clinic_id=rbac_graph.clinic_a, principal_id=principal.pk)
        revoke_principal(clinic_id=rbac_graph.clinic_a, principal_id=principal.pk)
        with pytest.raises(CurrentActorError):
            grant_principal(
                clinic_id=rbac_graph.clinic_a,
                principal_id=principal.pk,
                permission="appointment.read",
                subject_scope="clinic",
            )
    events = AuditEvent.objects.filter(
        affected_record_id__in=[str(principal.pk), str(grant.pk)]
    )
    assert set(events.values_list("event_type", flat=True)) == {
        "identity.principal.created",
        "identity.principal_grant.created",
        "identity.principal.revoked",
        "identity.principal_grant.revoked",
    }
    assert events.count() == 4


def test_runtime_and_unauthorized_owner_cannot_provision(rbac_graph: RbacGraph) -> None:
    for runtime in (False, True):
        # The receptionist remains unauthorized even on the owner connection.
        with (
            transaction.atomic(),
            owner_context(rbac_graph.organization_a),
            runtime_role() if runtime else transaction.atomic(),
            pytest.raises(CurrentActorError),
        ):
            register_principal(
                clinic_id=rbac_graph.clinic_a,
                name="sintetico-reader",
                db_identity="clinic_agent",
                purpose="availability",
            )
    with owner_context(rbac_graph.organization_a):
        assert not ServicePrincipal.objects.exists()


@pytest.mark.parametrize("foreign", [True, False])
def test_unknown_and_foreign_principals_share_denial(
    rbac_graph: RbacGraph, foreign: bool
) -> None:
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            role="owner",
        )
        principal = ServicePrincipal.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            name="sintetico-other",
            db_identity="clinic_agent",
            purpose="availability",
        )
    with (
        provisioning_context(rbac_graph, rbac_graph.shared_user),
        pytest.raises(CurrentActorError, match="current actor unauthorized"),
    ):
        revoke_principal(
            clinic_id=rbac_graph.clinic_a,
            principal_id=principal.pk if foreign else uuid4(),
        )


# Every public service in the provisioning module, derived rather than listed.
PROVISIONING = sorted(
    name
    for name, value in vars(service_principals).items()
    if inspect.isfunction(value)
    and value.__module__ == service_principals.__name__
    and not name.startswith("_")
)
ACTOR_STATES = (*UserClinicRole.Role.values, "none", "inactive_owner")


def _allowed(state: str) -> bool:
    return state in BUNDLES_V1 and "staff.organization" in BUNDLES_V1[state]


def _state_actor(graph: RbacGraph, state: str) -> UUID:
    actor, _ = permission_actor(graph, "owner" if state == "inactive_owner" else state)
    if state == "inactive_owner":
        User.objects.filter(pk=actor).update(is_active=False)
    return actor


def _provisioning_seed(graph: RbacGraph) -> dict[str, Callable[[], object]]:
    """One valid call per service; each call is otherwise legal for an owner."""
    with owner_context(graph.organization_a):
        granted, ungranted = (
            ServicePrincipal.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                name=f"sintetico-matrix-{suffix}",
                db_identity=f"clinic_agent_{uuid4().hex}",
                purpose="availability",
            )
            for suffix in ("granted", "ungranted")
        )
        grant = ServicePrincipalGrant.objects.create(
            organization_id=graph.organization_a,
            principal=granted,
            permission="appointment.read",
            subject_scope="clinic",
        )
    clinic = graph.clinic_a
    return {
        "grant_principal": lambda: service_principals.grant_principal(
            clinic_id=clinic,
            principal_id=ungranted.pk,
            permission="appointment.read",
            subject_scope="clinic",
        ),
        "register_principal": lambda: service_principals.register_principal(
            clinic_id=clinic,
            name="sintetico-matrix-new",
            db_identity=f"clinic_agent_{uuid4().hex}",
            purpose="availability",
        ),
        "revoke_principal": lambda: service_principals.revoke_principal(
            clinic_id=clinic, principal_id=granted.pk
        ),
        "revoke_principal_grant": lambda: service_principals.revoke_principal_grant(
            clinic_id=clinic, grant_id=grant.pk
        ),
    }


def _authority_state(
    graph: RbacGraph,
) -> tuple[frozenset[object], frozenset[object], int]:
    with owner_context(graph.organization_a):
        return (
            frozenset(ServicePrincipal.objects.values_list("pk", "active")),
            frozenset(ServicePrincipalGrant.objects.values_list("pk", "active")),
            AuditEvent.objects.filter(
                event_type__startswith="identity.principal"
            ).count(),
        )


def test_provisioning_authority_comes_from_the_role_catalog() -> None:
    assert {state for state in ACTOR_STATES if _allowed(state)} == {
        "owner",
        "org_admin",
    }
    assert {"physician", "clinic_admin", "receptionist", "none"} <= set(ACTOR_STATES)


@pytest.mark.parametrize("state", ACTOR_STATES)
def test_every_provisioning_service_requires_organization_staff_authority(
    rbac_graph: RbacGraph, state: str
) -> None:
    calls = _provisioning_seed(rbac_graph)
    assert sorted(calls) == PROVISIONING
    actor = _state_actor(rbac_graph, state)
    for name in PROVISIONING:
        before = _authority_state(rbac_graph)
        if _allowed(state):
            with provisioning_context(rbac_graph, actor):
                calls[name]()
            after = _authority_state(rbac_graph)
            assert after[:2] != before[:2], name
            assert after[2] == before[2] + 1, name
        else:
            with (
                pytest.raises(CurrentActorError),
                provisioning_context(rbac_graph, actor),
            ):
                calls[name]()
            assert _authority_state(rbac_graph) == before, name

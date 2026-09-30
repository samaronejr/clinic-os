"""Executed adapters for labels that delegate straight to an authority root.

Every case seeds real subjects in the actor's clinic, so the only possible
refusal is authority, never "not found". The census seeds each case inside its
scenario, runs it with its membership-free actor (or, for session cases, a
member of a sibling clinic) and requires the named root to execute. The
decision test (test_delegation_probes.py) runs each case with an actor that
holds the permission, which must proceed, and with real staff that lack it,
which must be refused by the permission check itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self
from unittest import mock
from uuid import uuid4

from apps.identity import scope_provisioning, service_principals
from apps.identity.current_context import _UnauthorizedActorError, require_permission
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    ServicePrincipal,
    ServicePrincipalGrant,
    User,
    UserClinicRole,
)
from apps.realtime import authorization, scopes
from apps.realtime.topics import TOPIC_PERMISSIONS
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.core import signing
from django.db import connection, transaction
from django.test import override_settings
from django_otp import DEVICE_ID_SESSION_KEY
from psycopg import sql

from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context, permission_actor
from otp_test_support import create_totp_device

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph

# Default deciding permission; agenda subscriptions use their topic permission.
PERMISSION = "staff.organization"


@dataclass(frozen=True)
class Seeded:
    """A call on real subjects plus the observable state it may change."""

    call: Callable[[], object]
    state: Callable[[], object]
    # For calls without a stored effect: the value proceeding must return,
    # given the permitted actor.
    returns: Callable[[UUID], object] | None = None
    # The exception a refusal surfaces as; its chain must hold the
    # require_permission denial (see test_delegation_probes).
    denial: type[Exception] = _UnauthorizedActorError


@dataclass(frozen=True)
class Case:
    """How a root delegation is driven and which permission decides it."""

    seed: Callable[[RbacGraph, UUID], Seeded]
    database_role: str = "clinic_owner"
    permission: str = PERMISSION
    # The actor comes from a stored login session and the call opens its own
    # tenant transaction, so it runs outside any enclosing transaction.
    session: bool = False


def _principal(graph: RbacGraph, *, granted: bool) -> ServicePrincipal:
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            name="sintetico-delegation",
            db_identity=f"clinic_agent_{uuid4().hex}",
            purpose="availability",
        )
        if granted:
            ServicePrincipalGrant.objects.create(
                organization_id=graph.organization_a,
                principal=principal,
                permission="appointment.read",
                subject_scope="clinic",
            )
    return principal


def _owned(graph: RbacGraph, read: Callable[[], object]) -> Callable[[], object]:
    def state() -> object:
        with owner_context(graph.organization_a):
            return read()

    return state


def _care_team(graph: RbacGraph, _actor: UUID) -> Seeded:
    clinician, _ = permission_actor(graph, "nurse")
    with owner_context(graph.organization_a):
        membership = CareTeamMembership.objects.get(user_id=clinician)
    return Seeded(
        lambda: scope_provisioning.revoke_care_team(
            clinic_id=graph.clinic_a, membership_id=membership.pk
        ),
        _owned(
            graph,
            lambda: CareTeamMembership.objects.get(pk=membership.pk).revoked_at,
        ),
    )


def _professional(graph: RbacGraph, _actor: UUID) -> Seeded:
    clinician, _ = permission_actor(graph, "nurse")
    with owner_context(graph.organization_a):
        registration = ProfessionalRegistration.objects.get(user_id=clinician)
    return Seeded(
        lambda: scope_provisioning.revoke_professional(
            clinic_id=graph.clinic_a, registration_id=registration.pk
        ),
        _owned(
            graph,
            lambda: ProfessionalRegistration.objects.get(pk=registration.pk).revoked_at,
        ),
    )


def _grant(graph: RbacGraph, _actor: UUID) -> Seeded:
    principal = _principal(graph, granted=False)
    return Seeded(
        lambda: service_principals.grant_principal(
            clinic_id=graph.clinic_a,
            principal_id=principal.pk,
            permission="appointment.read",
            subject_scope="clinic",
        ),
        _owned(
            graph,
            lambda: ServicePrincipalGrant.objects.filter(principal=principal).count(),
        ),
    )


def _revoke(graph: RbacGraph, _actor: UUID) -> Seeded:
    principal = _principal(graph, granted=True)
    return Seeded(
        lambda: service_principals.revoke_principal(
            clinic_id=graph.clinic_a, principal_id=principal.pk
        ),
        _owned(graph, lambda: ServicePrincipal.objects.get(pk=principal.pk).active),
    )


def _revoke_grant(graph: RbacGraph, _actor: UUID) -> Seeded:
    principal = _principal(graph, granted=True)
    with owner_context(graph.organization_a):
        grant = ServicePrincipalGrant.objects.get(principal=principal)
    return Seeded(
        lambda: service_principals.revoke_principal_grant(
            clinic_id=graph.clinic_a, grant_id=grant.pk
        ),
        _owned(graph, lambda: ServicePrincipalGrant.objects.get(pk=grant.pk).active),
    )


def _owner_clinic(graph: RbacGraph, _actor: UUID) -> Seeded:
    # No subject: proceeding means returning the authorized clinic.
    return Seeded(
        lambda: scope_provisioning._owner_clinic(graph.clinic_a).pk,
        lambda: None,
        returns=lambda _actor: graph.clinic_a,
    )


def _require_permission(graph: RbacGraph, _actor: UUID) -> Seeded:
    return Seeded(
        lambda: require_permission(PERMISSION, clinic_id=graph.clinic_a),
        lambda: None,
        returns=lambda actor: actor,
    )


@contextmanager
def _as_owner() -> Iterator[None]:
    """Trusted seeding on the owner role, then back to the bound runtime role."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        (row,) = cursor.fetchall()
        (role,) = row
        cursor.execute("SET ROLE clinic_owner")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))


def staff_session(graph: RbacGraph, actor: UUID) -> str:
    """A real, fully verified staff login session (TOTP bound) for the actor."""
    with _as_owner():
        user = User.objects.get(pk=actor)
        device = create_totp_device(actor, confirmed=True)
        session = SessionStore()
        session[SESSION_KEY] = str(actor)
        session[BACKEND_SESSION_KEY] = "apps.identity.auth_backends.ClinicBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session["active_org_id"] = str(graph.organization_a)
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
    assert session.session_key is not None
    return session.session_key


def _grant_topic(graph: RbacGraph, _actor: UUID) -> Seeded:
    # A real member of the clinic receives the lease.
    return Seeded(
        lambda: scopes.grant_clinic_topic(
            clinic_id=graph.clinic_a,
            user_id=graph.physician,
            kind="messages",
            permission="appointment.read",
        ),
        lambda: None,
        returns=lambda _permitted: f"clinic:{graph.clinic_a}:messages",
    )


def _revoke_topic(graph: RbacGraph, _actor: UUID) -> Seeded:
    def call() -> int:
        scheduled = len(connection.run_on_commit)
        scopes.revoke_clinic_topic(
            clinic_id=graph.clinic_a, user_id=graph.physician, kind="messages"
        )
        # Proceeding schedules the Redis removal; never let it reach a broker.
        scheduled = len(connection.run_on_commit) - scheduled
        transaction.set_rollback(True)
        return scheduled

    return Seeded(call, lambda: None, returns=lambda _permitted: 1)


class _LeaseStore:
    """A lease an administrator issued earlier, keyed to the requesting actor.

    Only the transport cache is replaced, never the authority: authorize_scope
    still verifies the signature, topic and actor, then reruns the lease's
    permission in the database. No broker or host Redis is ever contacted.
    """

    def __init__(self, topic: str, clinic: UUID) -> None:
        self.topic = topic
        self.clinic = clinic

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, key: str) -> bytes | None:
        prefix = scopes._key(self.topic, None) + ":"
        if not key.startswith(prefix):
            return None
        return signing.dumps(
            {
                "topic": self.topic,
                "user": key.removeprefix(prefix),
                "clinic": str(self.clinic),
                "permission": PERMISSION,
                "enrollment": None,
            },
            salt=scopes.SCOPE_SALT,
        ).encode("ascii")


def _authorize_scope(graph: RbacGraph, _actor: UUID) -> Seeded:
    topic = f"clinic:{graph.clinic_a}:inbox"
    store = _LeaseStore(topic, graph.clinic_a)

    def call() -> object:
        with (
            override_settings(REALTIME_ENABLED=True),
            mock.patch.object(scopes, "redis_client", return_value=store),
        ):
            return scopes.authorize_scope(topic=topic)

    return Seeded(call, lambda: None, returns=lambda _permitted: graph.clinic_a)


def _verified_staff(graph: RbacGraph, actor: UUID) -> Seeded:
    key = staff_session(graph, actor)
    return Seeded(
        lambda: authorization._verified_staff(
            SessionStore(session_key=key), (f"clinic:{graph.clinic_a}:agenda",)
        ),
        lambda: None,
        returns=lambda permitted: permitted,
    )


def _authorize_topics(graph: RbacGraph, actor: UUID) -> Seeded:
    key = staff_session(graph, actor)
    return Seeded(
        lambda: (
            authorization.authorize_topics_sync(
                session_key=key, topics=(f"clinic:{graph.clinic_a}:agenda",)
            ).user_id
        ),
        lambda: None,
        returns=lambda permitted: permitted,
        denial=authorization.TopicDeniedError,
    )


AGENDA = TOPIC_PERMISSIONS["agenda"]
CASES: dict[str, Case] = {
    "apps.identity.current_context.require_permission": Case(
        _require_permission, database_role="clinic_app"
    ),
    "apps.identity.scope_provisioning._owner_clinic": Case(_owner_clinic),
    "apps.identity.scope_provisioning.revoke_care_team": Case(_care_team),
    "apps.identity.scope_provisioning.revoke_professional": Case(_professional),
    "apps.identity.service_principals.grant_principal": Case(_grant),
    "apps.identity.service_principals.revoke_principal": Case(_revoke),
    "apps.identity.service_principals.revoke_principal_grant": Case(_revoke_grant),
    # The realtime root delegations (todo 8), executed on real subjects as well.
    "apps.realtime.scopes.grant_clinic_topic": Case(
        _grant_topic, database_role="clinic_app"
    ),
    "apps.realtime.scopes.revoke_clinic_topic": Case(
        _revoke_topic, database_role="clinic_app"
    ),
    "apps.realtime.scopes.authorize_scope": Case(
        _authorize_scope, database_role="clinic_app"
    ),
    "apps.realtime.authorization._verified_staff": Case(
        _verified_staff, database_role="clinic_app", permission=AGENDA, session=True
    ),
    "apps.realtime.authorization.authorize_topics_sync": Case(
        _authorize_topics, database_role="clinic_app", permission=AGENDA, session=True
    ),
}


def census_member(graph: RbacGraph) -> UUID:
    """A real organization member without any role in the target clinic.

    Tenant entry admits them, so the refusal can only come from the target
    clinic's permission check. The sibling clinic keeps target-clinic member
    sets unchanged for the other census probes, and no care-team rows exist.
    """
    user = User.objects.create(username=f"synthetic-delegation-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    return user.pk


class _Deferred:
    """Seed inside the census scenario, then call the real subject.

    Session cases own their tenant transaction, so their seed commits: a
    minimal organization member logs in, because the census actor has no
    membership and would be refused at tenant entry instead.
    """

    def __init__(self, graph: RbacGraph, actor: UUID, case: Case) -> None:
        self.graph = graph
        self.actor = actor
        self.case = case
        self.seeded: Seeded | None = None

    def prepare(self) -> None:
        with _as_owner():
            actor = census_member(self.graph) if self.case.session else self.actor
            self.seeded = self.case.seed(self.graph, actor)

    def invoke(self) -> object:
        assert self.seeded is not None
        try:
            return self.seeded.call()
        except self.seeded.denial:
            return False


def delegation_probes(graph: RbacGraph, actor: UUID) -> list[DifferentialProbe]:
    probes = []
    for symbol, case in CASES.items():
        deferred = _Deferred(graph, actor, case)
        probes.append(
            DifferentialProbe(
                symbol,
                deferred.invoke,
                expected=False,
                database_role=case.database_role,
                owns_transaction=case.session,
                prepare=deferred.prepare,
            )
        )
    return probes

"""Owner-only authority mutations append metadata audit in the same transaction."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.identity.current_context import CurrentActorError
from apps.identity.models import RoleGrant, UserClinicRole
from apps.identity.scope_provisioning import (
    CareTeamAssignment,
    ProfessionalIdentity,
    assign_care_team,
    narrow_role,
    register_professional,
    revoke_care_team,
    revoke_professional,
)
from django.db import connection
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def provisioning_context(graph: RbacGraph, actor: UUID) -> Iterator[None]:
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
        cursor.execute("SELECT set_config('app.current_user_id',%s,true)", [str(actor)])
        yield


def test_owner_mutations_are_audited_and_revoke_is_idempotent(
    rbac_graph: RbacGraph,
) -> None:
    owner, _ = permission_actor(rbac_graph, "owner")
    clinician, enrollment = permission_actor(rbac_graph, "nurse")
    start = timezone.now()
    with provisioning_context(rbac_graph, owner):
        removal = narrow_role(
            clinic_id=rbac_graph.clinic_a,
            role="finance",
            permission="charge.read",
            valid_from=start,
        )
        team = assign_care_team(
            clinic_id=rbac_graph.clinic_a,
            assignment=CareTeamAssignment(
                user_id=clinician,
                patient_enrollment_id=enrollment,
                role="nurse",
            ),
            valid_from=start,
        )
        registration = register_professional(
            clinic_id=rbac_graph.clinic_a,
            identity=ProfessionalIdentity(
                user_id=clinician,
                role="nurse",
                council="COREN",
                number="SINTETICO-AUDIT-SENTINEL",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
            ),
            valid_from=start,
            valid_to=start + timedelta(days=1),
        )
        revoke_care_team(clinic_id=rbac_graph.clinic_a, membership_id=team.pk)
        revoke_care_team(clinic_id=rbac_graph.clinic_a, membership_id=team.pk)
        revoke_professional(
            clinic_id=rbac_graph.clinic_a, registration_id=registration.pk
        )
        revoke_professional(
            clinic_id=rbac_graph.clinic_a, registration_id=registration.pk
        )
        events = list(
            AuditEvent.objects.filter(event_type__startswith="identity.")
            .order_by("seq")
            .values_list("event_type", "affected_record_id", "payload")
        )
        assert [(event, affected) for event, affected, _ in events] == [
            ("identity.role_grant.created", str(removal.pk)),
            ("identity.care_team.created", str(team.pk)),
            ("identity.professional_registration.created", str(registration.pk)),
            ("identity.care_team.revoked", str(team.pk)),
            ("identity.professional_registration.revoked", str(registration.pk)),
        ]
        assert all(
            set(payload) == {"clinic_id", "object_verb"} for _, _, payload in events
        )
        assert "SINTETICO-AUDIT-SENTINEL" not in str(events)


def test_runtime_owner_cannot_mutate_authority(rbac_graph: RbacGraph) -> None:
    owner, _ = permission_actor(rbac_graph, "owner")
    with permission_context(rbac_graph, owner), pytest.raises(CurrentActorError):
        narrow_role(
            clinic_id=rbac_graph.clinic_a,
            role="finance",
            permission="charge.read",
            valid_from=timezone.now(),
        )


def test_owner_connection_still_requires_current_clinic_authority(
    rbac_graph: RbacGraph,
) -> None:
    with (
        provisioning_context(rbac_graph, rbac_graph.shared_user),
        pytest.raises(CurrentActorError),
    ):
        narrow_role(
            clinic_id=rbac_graph.clinic_a,
            role="finance",
            permission="charge.read",
            valid_from=timezone.now(),
        )


def test_failed_audit_rolls_back_authority_change(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = permission_actor(rbac_graph, "owner")

    def unavailable(*args: object, **kwargs: object) -> int:
        message = "synthetic audit outage"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "apps.identity.scope_provisioning.record_phase1_event", unavailable
    )
    with provisioning_context(rbac_graph, owner):
        with pytest.raises(RuntimeError, match="synthetic audit outage"):
            narrow_role(
                clinic_id=rbac_graph.clinic_a,
                role="finance",
                permission="charge.read",
                valid_from=timezone.now(),
            )
        assert not RoleGrant.objects.exists()
        assert UserClinicRole.objects.filter(user_id=owner).exists()


@pytest.mark.parametrize("kind", ["care", "professional"])
def test_revoke_unknown_and_wrong_clinic_share_the_denial(
    rbac_graph: RbacGraph,
    kind: str,
) -> None:
    owner, _ = permission_actor(rbac_graph, "owner")
    operation = revoke_care_team if kind == "care" else revoke_professional
    selector = "membership_id" if kind == "care" else "registration_id"
    with provisioning_context(rbac_graph, owner):
        for clinic in (rbac_graph.clinic_a, rbac_graph.clinic_b):
            with pytest.raises(CurrentActorError) as error:
                operation(clinic_id=clinic, **{selector: uuid4()})
            assert error.value.args == ("current actor unauthorized",)

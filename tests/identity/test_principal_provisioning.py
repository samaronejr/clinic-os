"""Owner-provisioned principals retain immutable, audited authority history."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.identity.current_context import CurrentActorError
from apps.identity.models import ServicePrincipal, UserClinicRole
from apps.identity.service_principals import (
    grant_principal,
    register_principal,
    revoke_principal,
    revoke_principal_grant,
)
from django.db import transaction

from identity.permission_support import owner_context
from identity.test_scope_provisioning import provisioning_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


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

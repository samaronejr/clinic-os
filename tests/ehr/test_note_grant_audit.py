"""SC-4: identity loss retains one atomic audit receipt per revoked note grant."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Literal, assert_never

import pytest
from apps.audit.models import AuditEvent
from apps.audit.services import verify_chain
from apps.ehr.models import NoteAccessGrant
from apps.ehr.note_access import grant_note_access, restrict_note, revoke_note_access
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import LifecycleContext, scoped_lifecycle_gucs
from apps.identity.management.revocation import (
    RevokeStaffRoleRequest,
    revoke_staff_role,
)
from apps.identity.models import User, UserClinicRole
from apps.identity.offboarding import offboard_staff
from django.db import connection, transaction

from ehr.longitudinal_support import chart_world, finalized, second_draft
from ehr.test_addenda import clinician
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
type RecipientLoss = Literal["role", "inactive", "offboard"]


def _grants(graph: RbacGraph) -> tuple[UUID, tuple[UUID, ...]]:
    world = chart_world(graph)
    sources = (
        finalized(graph, world.version),
        finalized(graph, second_draft(graph, world, graph.physician)),
    )
    recipient = clinician(graph, "physician", world.enrollment)
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role=UserClinicRole.Role.OWNER,
        )
    grant_ids: list[UUID] = []
    with as_actor(graph, graph.physician):
        for source in sources:
            restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
            grant_ids.append(
                grant_note_access(
                    clinic_id=graph.clinic_a,
                    version_id=source.pk,
                    grantee_id=recipient,
                ).pk
            )
    return recipient, tuple(grant_ids)


def _lose_recipient(graph: RbacGraph, recipient: UUID, loss: RecipientLoss) -> None:
    match loss:
        case "role":
            revoke_staff_role(
                LifecycleContext(
                    operator_id=graph.clinic_admin,
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                ),
                RevokeStaffRoleRequest(
                    target_user_id=recipient, role=UserClinicRole.Role.PHYSICIAN
                ),
            )
        case "inactive":
            with owner_context(graph.organization_a), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_user_id', %s, true)",
                    [str(graph.clinic_admin)],
                )
                User.objects.filter(pk=recipient).update(is_active=False)
        case "offboard":
            with (
                transaction.atomic(),
                scoped_lifecycle_gucs(graph.organization_a, graph.clinic_admin),
                runtime_role(),
            ):
                offboard_staff(clinic_id=graph.clinic_a, user_id=recipient)
        case unreachable:
            assert_never(unreachable)


def _events(graph: RbacGraph) -> list[AuditEvent]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(
                organization_id=graph.organization_a,
                event_type="ehr.note.revoked",
            ).order_by("seq")
        )


def _assert_events(graph: RbacGraph, grant_ids: tuple[UUID, ...], actor: UUID) -> None:
    with owner_context(graph.organization_a):
        assert NoteAccessGrant.objects.filter(
            pk__in=grant_ids, revoked_at__isnull=False
        ).count() == len(grant_ids)
    events = _events(graph)
    assert Counter(event.affected_record_id for event in events) == Counter(
        str(grant_id) for grant_id in grant_ids
    )
    for event in events:
        assert event.actor_user_id == actor
        assert event.affected_record_type == "ehr.note_access_grant"
        assert event.component_id == "clinic-os-web"
        assert event.component_ip is None
        assert event.payload == {
            "clinic_id": str(graph.clinic_a),
            "object_verb": "revoked",
        }
    with as_actor(graph, graph.clinic_admin):
        assert verify_chain(graph.organization_a).valid


@pytest.mark.parametrize("loss", ["role", "inactive", "offboard"])
def test_identity_loss_audits_each_revoked_note_grant(
    rbac_graph: RbacGraph, loss: RecipientLoss
) -> None:
    # Given two separately restricted notes granted to one eligible recipient.
    graph = rbac_graph
    recipient, grant_ids = _grants(graph)

    # When an actual identity lifecycle entrypoint removes eligibility.
    _lose_recipient(graph, recipient, loss)

    # Then each retained transition has its own metadata-only, chain-valid event.
    _assert_events(graph, grant_ids, graph.clinic_admin)


@pytest.mark.parametrize("loss", ["role", "inactive", "offboard"])
def test_identity_loss_replay_does_not_duplicate_note_grant_audit(
    rbac_graph: RbacGraph, loss: RecipientLoss
) -> None:
    # Given grants already revoked through the real lifecycle entrypoint.
    graph = rbac_graph
    recipient, grant_ids = _grants(graph)
    _lose_recipient(graph, recipient, loss)

    # When the same lifecycle request is replayed (retired roles are refused).
    match loss:
        case "role":
            with pytest.raises(LifecycleCommandError):
                _lose_recipient(graph, recipient, loss)
        case "inactive" | "offboard":
            _lose_recipient(graph, recipient, loss)
        case unreachable:
            assert_never(unreachable)

    # Then no second grant transition or receipt exists.
    _assert_events(graph, grant_ids, graph.clinic_admin)


@pytest.mark.parametrize("loss", ["role", "inactive", "offboard"])
def test_identity_loss_rollback_reverts_note_grant_and_audit_together(
    rbac_graph: RbacGraph, loss: RecipientLoss
) -> None:
    # Given live retained grants and no revocation receipts.
    graph = rbac_graph
    recipient, grant_ids = _grants(graph)

    # When the enclosing transaction rolls back a real identity transition.
    with transaction.atomic():
        _lose_recipient(graph, recipient, loss)
        assert len(_events(graph)) == 2
        transaction.set_rollback(True)

    # Then neither revocation nor its audit receipt survived.
    with owner_context(graph.organization_a):
        assert not NoteAccessGrant.objects.filter(
            pk__in=grant_ids, revoked_at__isnull=False
        ).exists()
    assert _events(graph) == []


def test_explicit_note_grant_revocation_remains_exactly_once(
    rbac_graph: RbacGraph,
) -> None:
    # Given one explicitly revoked grant and another live grant.
    graph = rbac_graph
    _, grant_ids = _grants(graph)
    with as_actor(graph, graph.physician):
        revoke_note_access(clinic_id=graph.clinic_a, grant_id=grant_ids[0])

    # When the author retries the explicit revocation.
    with as_actor(graph, graph.physician):
        revoke_note_access(clinic_id=graph.clinic_a, grant_id=grant_ids[0])

    # Then the shared transition writer did not double the existing receipt.
    _assert_events(graph, (grant_ids[0],), graph.physician)

"""Emergency reads retain professional limits, notices and mandatory review."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.audit.models import AuditEvent
from apps.identity import enterprise_authority as clock
from apps.identity.enterprise_access import (
    EmergencyScope,
    GovernanceDeniedError,
    grant_break_glass,
    read_break_glass,
)
from apps.identity.models import BreakGlassGrant, GovernanceNotice
from apps.workflows.models import Task
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_break_glass_without_reason_leaves_no_grant(rbac_graph: RbacGraph) -> None:
    # Given a currently registered clinician and another patient's enrollment.
    clinician, _own = permission_actor(rbac_graph, "physician")
    _other, enrollment = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    with permission_context(rbac_graph, clinician):
        # When emergency authority is requested without its required reason.
        with pytest.raises(GovernanceDeniedError):
            grant_break_glass(
                clinic_id=rbac_graph.clinic_a,
                scope=EmergencyScope(enrollment),
                reason=" ",
                expires_at=timezone.now() + timedelta(hours=1),
                session_key=session_key(client),
            )
        # Then refusal persists no grant or notification.
        assert not BreakGlassGrant.objects.exists()
        assert not GovernanceNotice.objects.exists()


@pytest.mark.parametrize("hours", [0, 5])
def test_break_glass_rejects_unbounded_or_elapsed_expiry(
    rbac_graph: RbacGraph, hours: int
) -> None:
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    with (
        permission_context(rbac_graph, clinician),
        pytest.raises(GovernanceDeniedError),
    ):
        grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="Synthetic emergency",
            expires_at=timezone.now() + timedelta(hours=hours),
            session_key="not-a-session",
        )


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_break_glass_commits_manager_and_patient_notices_review_and_safe_audit(
    rbac_graph: RbacGraph,
) -> None:
    # Given a real verified clinician outside this patient's care team.
    enterprise_setup(rbac_graph)
    clinician, _own = permission_actor(rbac_graph, "physician")
    _other, enrollment = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    # When emergency access is issued.
    with permission_context(rbac_graph, clinician):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="SINTETICO-SENTINELA-emergency-reason",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(client),
        )
        snapshot = read_break_glass(clinic_id=rbac_graph.clinic_a, grant_id=grant.pk)
    # Then its exact scope, mandatory task and both notification audiences persist.
    assert snapshot.enrollment_id == enrollment
    assert grant.review_task_id is not None
    with owner_context(rbac_graph.organization_a):
        assert Task.objects.filter(pk=grant.review_task_id, kind="review").exists()
        assert set(
            GovernanceNotice.objects.filter(break_glass=grant).values_list(
                "audience", flat=True
            )
        ) == {"manager", "patient"}
        events = list(
            AuditEvent.objects.filter(affected_record_id=str(grant.pk)).values_list(
                "event_type", "payload"
            )
        )
    assert {name for name, _payload in events} >= {
        "identity.break_glass.granted",
        "identity.break_glass.read",
    }
    for _name, payload in events:
        assert set(payload) <= {
            "clinic_id",
            "http_method",
            "http_status",
            "object_verb",
            "reason_code",
            "request_id",
        }
        assert "SINTETICO-SENTINELA" not in str(payload)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
@pytest.mark.parametrize("elapsed", [timedelta(hours=1), timedelta(hours=4)])
def test_expired_break_glass_cannot_read_with_a_monkeypatched_clock(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, elapsed: timedelta
) -> None:
    # Given a persisted grant and real factor proof; no timer wait is used.
    enterprise_setup(rbac_graph)
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    issued = timezone.now()
    with permission_context(rbac_graph, clinician):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="Synthetic emergency",
            expires_at=issued + timedelta(hours=1),
            session_key=session_key(client),
        )
    # When the read clock reaches or passes the exclusive expiry boundary.
    monkeypatch.setattr(clock, "utc_now", lambda: issued + elapsed)
    with (
        permission_context(rbac_graph, clinician),
        pytest.raises(GovernanceDeniedError),
    ):
        read_break_glass(clinic_id=rbac_graph.clinic_a, grant_id=grant.pk)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_break_glass_cannot_use_someone_elses_verified_session(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    clinician, enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, clinician):
        with pytest.raises(GovernanceDeniedError):
            grant_break_glass(
                clinic_id=rbac_graph.clinic_a,
                scope=EmergencyScope(enrollment),
                reason="Synthetic emergency",
                expires_at=timezone.now() + timedelta(hours=1),
                session_key=setup.session_key,
            )
        assert not BreakGlassGrant.objects.exists()

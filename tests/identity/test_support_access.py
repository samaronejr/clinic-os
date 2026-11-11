"""Customer-approved support never borrows a clinical staff context."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity import enterprise_authority as clock
from apps.identity.enterprise_access import (
    GovernanceDeniedError,
    approve_support_access,
    support_diagnostics,
)
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup
from identity.permission_support import permission_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_support_is_masked_and_purpose_bound(rbac_graph: RbacGraph) -> None:
    # Given an explicit customer-admin approval under actual recent TOTP.
    setup = enterprise_setup(rbac_graph)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.clinic_admin,
            purpose="Synthetic integration diagnosis",
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    # When the named support user opens the allowlisted diagnostic projection.
    with runtime_role():
        result = support_diagnostics(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=setup.session_key,
        )
    # Then no identity, patient record or clinical write capability is exposed.
    assert result.masked is True
    assert result.clinical_write is False
    assert result.providers
    assert all(set(row) == {"capability", "state"} for row in result.providers)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_jit_expiry_uses_a_clock_not_a_wait(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a short-lived approved grant.
    setup = enterprise_setup(rbac_graph)
    expires = timezone.now() + timedelta(minutes=10)
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        grant = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.clinic_admin,
            purpose="Synthetic diagnostic",
            expires_at=expires,
            session_key=setup.session_key,
        )
    # When the clock reaches the exclusive upper bound.
    monkeypatch.setattr(clock, "utc_now", lambda: expires)
    # Then the same still-valid browser session cannot read through the grant.
    with runtime_role(), pytest.raises(GovernanceDeniedError):
        support_diagnostics(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=setup.session_key,
        )

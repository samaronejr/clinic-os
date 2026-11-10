"""Clinic managers can inspect mandatory reviews without organization powers."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.identity.enterprise_access import EmergencyScope, grant_break_glass
from apps.identity.enterprise_support_access import approve_support_access
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_clinic_manager_can_read_reason_and_complete_only_an_ended_review(
    rbac_graph: RbacGraph,
) -> None:
    setup = enterprise_setup(rbac_graph)
    clinician, _own = permission_actor(rbac_graph, "physician")
    _other, enrollment = permission_actor(rbac_graph, "physician")
    manager, _manager_patient = permission_actor(rbac_graph, "clinic_admin")
    clinician_client = verified_actor_client(clinician)
    manager_client = verified_actor_client(manager)
    with permission_context(rbac_graph, clinician):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(enrollment),
            reason="SYNTHETIC-REVIEW-REASON",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(clinician_client),
        )
    with runtime_role():
        response = manager_client.get(
            f"/clinics/{rbac_graph.clinic_a}/settings/emergency-reviews/"
        )
    assert response.status_code == 200
    assert b"SYNTHETIC-REVIEW-REASON" in response.content
    assert "no-store" in response.headers["Cache-Control"]
    with runtime_role():
        response = manager_client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/grants/",
            {"grant_id": grant.pk, "action": "break_glass_review"},
        )
        assert response.status_code == 403
        # A command outside the closed grant actions is the view's own 400.
        response = manager_client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/grants/",
            {"grant_id": grant.pk, "action": "unknown"},
        )
        assert response.status_code == 400
        response = manager_client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/grants/",
            {"grant_id": grant.pk, "action": "break_glass_revoke"},
        )
        assert response.status_code == 200
        response = manager_client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/grants/",
            {"grant_id": grant.pk, "action": "break_glass_review"},
        )
        assert response.status_code == 200
    with permission_context(rbac_graph, manager):
        grant.refresh_from_db()
        assert grant.reviewed_at is not None
        assert grant.reviewed_by_id == manager
    with permission_context(rbac_graph, rbac_graph.clinic_admin):
        support = approve_support_access(
            clinic_id=rbac_graph.clinic_a,
            support_user_id=rbac_graph.clinic_admin,
            purpose="Synthetic support approval",
            expires_at=timezone.now() + timedelta(minutes=30),
            session_key=setup.session_key,
        )
    with runtime_role():
        response = manager_client.post(
            f"/clinics/{rbac_graph.clinic_a}/settings/governance/grants/",
            {"grant_id": support.pk, "action": "support_revoke"},
        )
    assert response.status_code == 403
    with owner_context(rbac_graph.organization_a):
        support.refresh_from_db()
        assert support.revoked_at is None


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_review_page_rechecks_manager_authority_after_the_factor_check(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
) -> None:
    # Given a unit manager whose verified session opens the review page.
    enterprise_setup(rbac_graph)
    manager, _manager_patient = permission_actor(rbac_graph, "clinic_admin")
    manager_client = verified_actor_client(manager)
    page = f"/clinics/{rbac_graph.clinic_a}/settings/emergency-reviews/"
    with runtime_role():
        assert manager_client.get(page).status_code == 200
    revoked: list[int] = []

    def revoke_before_recheck(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "clinic_app.enterprise_manager(" in sql and not revoked:
            # Another connection commits the membership revocation after
            # admission and the factor check; READ COMMITTED shows it to the
            # view's own SQL recheck. The lock timeout bounds a failure only.
            with psycopg.connect(
                superuser_database_url,
                dbname=str(connection.settings_dict["NAME"]),
                autocommit=True,
            ) as other:
                other.execute("SET lock_timeout = '10s'")
                changed = other.execute(
                    "UPDATE clinic_app.identity_userclinicrole "
                    "SET revoked_at=statement_timestamp() "
                    "WHERE user_id=%s AND clinic_id=%s AND revoked_at IS NULL",
                    [manager, rbac_graph.clinic_a],
                )
                revoked.append(changed.rowcount)
        return execute(sql, params, many, context)

    # When the page is requested again while that revocation lands.
    with runtime_role(), connection.execute_wrapper(revoke_before_recheck):
        refused = manager_client.get(page)
    # Then the view's own recheck refuses it.
    assert revoked == [1]
    assert refused.status_code == 403

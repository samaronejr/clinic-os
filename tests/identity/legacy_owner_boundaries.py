"""Actual owner-connection lifecycle guards, not harvested role tuples."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.management.base import assert_owner_database_role
from apps.identity.management.context import LifecycleContext
from apps.identity.management.provisioning import ProvisionStaffRequest, provision_staff
from apps.identity.management.revocation import (
    RevokeStaffRoleRequest,
    revoke_staff_role,
)
from apps.identity.models import UserClinicRole
from django.db import connection

from identity.legacy_parity_support import LEGACY, Boundary
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.legacy_parity_support import LegacyWorld


def _owner_call(action: Callable[[], object]) -> object:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_owner")
    try:
        return action()
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE clinic_app")


def _provision(w: LegacyWorld, valid: bool) -> object:
    context = LifecycleContext(w.actor.pk, w.graph.organization_a, w.clinic_for(valid))
    key = uuid4()
    request = ProvisionStaffRequest(
        key,
        f"synthetic-parity-{key.hex}",
        f"synthetic-{key.hex}@example.invalid",
        UserClinicRole.Role.RECEPTIONIST,
    )
    return _owner_call(lambda: provision_staff(context, request, RBAC_RAW_CREDENTIAL))


def _revoke(w: LegacyWorld, valid: bool) -> object:
    context = LifecycleContext(w.actor.pk, w.graph.organization_a, w.clinic)
    request = RevokeStaffRoleRequest(
        w.graph.shared_user if valid else w.actor.pk,
        UserClinicRole.Role.RECEPTIONIST if valid else UserClinicRole.Role.OWNER,
    )
    return _owner_call(lambda: revoke_staff_role(context, request))


def _database_role(_w: LegacyWorld, valid: bool) -> object:
    if valid:
        return _owner_call(assert_owner_database_role)
    assert_owner_database_role()
    return None


BOUNDARIES = (
    Boundary(
        "apps.identity.management.provisioning.provision_staff",
        "owner",
        ("owner",),
        _provision,
    ),
    Boundary(
        "apps.identity.management.revocation.revoke_staff_role",
        "owner",
        ("owner",),
        _revoke,
    ),
    Boundary(
        "apps.identity.management.base.assert_owner_database_role",
        "database_role",
        LEGACY,
        _database_role,
    ),
)

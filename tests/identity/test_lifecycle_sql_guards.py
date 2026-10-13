"""Direct current lifecycle SQL authority on real prepared staff subjects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.models import User
from apps.identity.session_inventory import session_hash
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.test import override_settings

from auth.test_reset_delay import reset_db_clock
from identity.legacy_lifecycle_boundaries import lifecycle_world
from identity.legacy_parity_support import world
from identity.sql_guard_probes import ALL_ROLES, STAFF_MANAGERS
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from rbac_fixtures import RbacGraph

__all__ = ("reset_db_clock",)
pytestmark = pytest.mark.django_db(transaction=True)
STAFF_PROTOCOL_GUARDS = (
    "clinic_app.identity_offboarding_clinics",
    "clinic_app.identity_offboarding_revoke",
    "clinic_app.identity_reset_execute",
    "clinic_app.identity_reset_recipient",
)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_current_staff_protocol_guards(
    rbac_graph: RbacGraph,
    role: str,
    reset_db_clock: Callable[[datetime], None],
) -> None:
    """SQL itself must refuse roles and foreign scope, not Python prechecks."""
    subject = world(rbac_graph, role)
    with override_settings(COMMS_SYNTHETIC_CHANNELS=("email",)):
        subject = lifecycle_world(subject, reset_db_clock)
    target = subject.lifecycle_target
    reset = subject.lifecycle_reset
    key = subject.request.session.session_key
    assert target is not None
    assert reset is not None
    assert key is not None
    recipient = User.objects.get(pk=target).email
    with runtime_role(), tenant_context(subject.actor.pk, rbac_graph.organization_a):
        for valid in (True, False):
            allowed = valid and role in STAFF_MANAGERS
            clinic = subject.clinic_for(valid)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.identity_offboarding_clinics(%s,%s)",
                    [target, clinic],
                )
                assert cursor.fetchone() == (
                    ([subject.clinic],) if allowed else (None,)
                )
                transaction.set_rollback(True)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.identity_offboarding_revoke(%s,%s)",
                    [target, clinic],
                )
                assert cursor.fetchone() == (allowed,)
                transaction.set_rollback(True)
            with transaction.atomic(), connection.cursor() as cursor:
                # A real reset belongs to this clinic. A missing reset exercises
                # the same no-information refusal as a foreign selection.
                selected = reset if valid else rbac_graph.organization_b
                cursor.execute(
                    "SELECT clinic_app.identity_reset_execute(%s,%s)",
                    [selected, session_hash(key)],
                )
                assert cursor.fetchone() == (allowed,)
                transaction.set_rollback(True)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.identity_reset_recipient(%s)",
                    [reset if valid else rbac_graph.organization_b],
                )
                assert cursor.fetchone() == ((recipient,) if allowed else (None,))
                transaction.set_rollback(True)

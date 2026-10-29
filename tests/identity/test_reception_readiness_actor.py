"""Conditional readiness readers are tested without Python actor entry guards."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole
from django.db import connection, transaction

from auth.stepup_test_support import create_role_actor
from identity.legacy_parity_support import world
from identity.test_sql_inactive_actor import _set_active
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("role", ["clinic_admin", "receptionist", "physician"])
def test_readiness_sql_refuses_inactive_existing_actor(
    rbac_graph: RbacGraph, role: str, record_property: Callable[[str, object], None]
) -> None:
    subject = world(rbac_graph, "physician")
    actor = (
        subject.actor
        if role == "physician"
        else create_role_actor(rbac_graph, UserClinicRole.Role(role))
    )
    decisions = []
    for active in (True, False, True):
        _set_active(actor.pk, active=active)
        # Bind raw GUCs: tenant_context would reject the inactive actor before
        # the SQL function, hiding a broken database authority check.
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true), "
                "set_config('app.current_tenant',%s,true)",
                [str(actor.pk), str(rbac_graph.organization_a)],
            )
            cursor.execute(
                "SELECT * FROM clinic_app.reception_readiness(%s,%s)",
                [rbac_graph.clinic_a, subject.appointment.pk],
            )
            decisions.append(bool(cursor.fetchall()))
    record_property("readiness_inactive_actor_decisions", json.dumps(decisions))
    assert decisions == [True, False, True]

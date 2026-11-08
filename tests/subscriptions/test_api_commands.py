from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from django.urls import reverse

from core.test_navigation import _client_for, _csrf, _reauthenticate
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_commercial_api_changes_cancels_and_rejects_invalid_command_shapes(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "org_admin")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user=user,
            role="org_admin",
        )
    _reauthenticate(client, user.pk, "org_admin")
    url = reverse("subscriptions:commercial")
    change = {
        "clinic_id": str(graph.clinic_a),
        "action": "change_plan",
        "plan_code": "core_operations",
        "expected_revision": 1,
        "command_id": str(uuid4()),
    }
    with runtime_role(), fixed_otp_time():
        changed = client.post(
            url,
            data=json.dumps(change),
            content_type="application/json",
            headers=_csrf(client),
        )
        conflict = client.post(
            url,
            data=json.dumps({**change, "command_id": str(uuid4())}),
            content_type="application/json",
            headers=_csrf(client),
        )
        for body in (
            "{",
            "[]",
            " " * 4097,
            json.dumps({**change, "expected_revision": "2"}),
            json.dumps({**change, "expected_revision": 2**40}),
            json.dumps({**change, "plan_code": 1}),
        ):
            invalid = client.post(
                url, data=body, content_type="application/json", headers=_csrf(client)
            )
            assert invalid.status_code == 400
        cancelled = client.post(
            url,
            data=json.dumps(
                {
                    "clinic_id": str(graph.clinic_a),
                    "action": "cancel",
                    "expected_revision": 2,
                    "command_id": str(uuid4()),
                }
            ),
            content_type="application/json",
            headers=_csrf(client),
        )
    assert changed.status_code == 200
    assert conflict.status_code == 409
    assert cancelled.status_code == 200
    assert cancelled.json()["result"]["state"] == "cancelled"

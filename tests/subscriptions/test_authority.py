from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.subscriptions.models import Subscription, SubscriptionEvent
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction
from django.urls import get_resolver, reverse

from core.test_navigation import _client_for, _csrf, _reauthenticate
from identity.permission_support import owner_context, permission_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role
from subscriptions.test_contracts import administrator
from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_sql_scope_and_commands_check_every_role_and_current_actor(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    for role in UserClinicRole.Role.values:
        actor = User.objects.create(username=f"synthetic-sub-role-{uuid4().hex}")
        with owner_context(graph.organization_a):
            for clinic in (graph.clinic_a, graph.clinic_b):
                UserClinicRole.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=clinic,
                    user=actor,
                    role=role,
                )
        for active in (True, False, True):
            User.objects.filter(pk=actor.pk).update(is_active=active)
            with permission_context(graph, actor.pk), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.subscription_scope(%s,false), "
                    "clinic_app.subscription_scope(%s,true), "
                    "clinic_app.subscription_entitled(%s,'ai_tokens_in',1), "
                    "clinic_app.subscription_seats(%s)",
                    [
                        graph.clinic_a,
                        graph.clinic_a,
                        graph.clinic_a,
                        graph.organization_a,
                    ],
                )
                row = cursor.fetchone()
                assert row is not None
                manages = active and role in {"owner", "org_admin"}
                assert row[0] == (graph.organization_a if active else None)
                assert row[1] == (graph.organization_a if manages else None)
                assert row[2] is active
                assert (row[3] > 0) is active
                if not manages:
                    with pytest.raises(DatabaseError), transaction.atomic():
                        cursor.execute(
                            "SELECT * FROM clinic_app.subscription_command("
                            "%s,'cancel','',1,%s)",
                            [graph.clinic_a, uuid4()],
                        )
                else:
                    with transaction.atomic():
                        cursor.execute(
                            "SELECT * FROM clinic_app.subscription_command("
                            "%s,'plan_changed','core_operations',1,%s)",
                            [graph.clinic_a, uuid4()],
                        )
                        assert cursor.fetchone() is not None
                        transaction.set_rollback(True)


def test_discovered_commercial_route_refuses_forged_entitlements_without_effects(
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
    routes = [
        route
        for route in workspace_routes(get_resolver())
        if route.name.startswith("subscriptions:")
    ]
    assert routes
    initial_session = dict(client.session)
    initial_events = SubscriptionEvent.objects.count()
    for route in routes:
        url = reverse(route.name, kwargs=route.kwargs)
        for body in (
            {"clinic_id": str(graph.clinic_c), "action": "read"},
            {"clinic_id": str(uuid4()), "action": "read"},
            {
                "clinic_id": str(graph.clinic_a),
                "action": "change_plan",
                "entitlements": {"ai_tokens_in": 10**12},
            },
            {},
        ):
            with runtime_role(), fixed_otp_time():
                response = client.post(
                    url,
                    data=json.dumps(body),
                    content_type="application/json",
                    headers=_csrf(client),
                )
            assert response.status_code == 403
            assert response.json() == {"code": "not_available"}
            assert not response.cookies
            assert dict(client.session) == initial_session
            assert SubscriptionEvent.objects.count() == initial_events
        with runtime_role(), fixed_otp_time():
            response = client.post(
                url,
                data=json.dumps({"clinic_id": str(graph.clinic_a), "action": "read"}),
                content_type="application/json",
                headers=_csrf(client),
            )
            without_csrf = client.post(
                url,
                data=json.dumps({"clinic_id": str(graph.clinic_a), "action": "read"}),
                content_type="application/json",
            )
        assert response.status_code == 200
        assert response.json()["subscription"]["provisional"] is True
        assert without_csrf.status_code == 403


def test_owner_cli_routes_synthetic_receipts_and_runtime_cannot_invoke_it(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    call_command("subscription_event", "bootstrap-synthetic")
    with owner_context(graph.organization_a):
        subscription = Subscription.objects.get(organization_id=graph.organization_a)
    arguments = [
        "apply",
        "--operator-id",
        str(actor),
        "--organization-id",
        str(graph.organization_a),
        "--clinic-id",
        str(graph.clinic_a),
        "--subscription-id",
        str(subscription.pk),
        "--event-id",
        str(uuid4()),
        "--action",
        "paid",
    ]
    assert call_command("subscription_event", *arguments) == "active"
    assert call_command("subscription_event", *arguments) == "active"
    with owner_context(graph.organization_a):
        assert Subscription.objects.get(pk=subscription.pk).revision == 2
    User.objects.filter(pk=actor).update(is_active=False)
    with pytest.raises(CommandError):
        call_command("subscription_event", *arguments)

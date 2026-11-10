from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.subscriptions.collection import bootstrap_synthetic_collection
from apps.subscriptions.models import Entitlement, Plan, Subscription
from apps.subscriptions.services import change_plan, receive_synthetic_event
from django.db import connection, transaction
from psycopg import sql

from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from uuid import UUID

    from apps.subscriptions.services import CommandResult

    from rbac_fixtures import RbacGraph


def event(
    graph: RbacGraph, actor: UUID, action: str, key: UUID | None = None
) -> CommandResult:
    bootstrap_synthetic_collection()
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_user_id',%s,true)", [str(actor)])
        subscription = Subscription.objects.get(organization_id=graph.organization_a)
        return receive_synthetic_event(
            clinic_id=graph.clinic_a,
            subscription_id=subscription.pk,
            event_id=key or uuid4(),
            action=action,
        )


@contextmanager
def clock(at: datetime) -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.subscription_clock()'::regprocedure)"
        )
        original = cursor.fetchone()
    assert original is not None
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                sql.SQL(
                    "CREATE OR REPLACE FUNCTION clinic_app.subscription_clock() "
                    "RETURNS timestamptz LANGUAGE sql VOLATILE "
                    "SET search_path=pg_catalog,clinic_app,pg_temp "
                    "AS {}"
                ).format(sql.Literal("SELECT '" + at.isoformat() + "'::timestamptz"))
            )
            cursor.execute("RESET ROLE")
        yield
    finally:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(original[0])
            cursor.execute("RESET ROLE")


def publish_quota(graph: RbacGraph, actor: UUID, meter: str, quota: int) -> None:
    previous = Plan.objects.filter(code="intelligence_usage").latest("version")
    plan = Plan.objects.create(
        code=previous.code, version=previous.version + 1, provisional=True
    )
    for allowance in Entitlement.objects.filter(plan=previous):
        Entitlement.objects.create(
            plan=plan,
            meter=allowance.meter,
            enabled=allowance.enabled,
            quota=quota if allowance.meter == meter else allowance.quota,
        )
    with permission_context(graph, actor):
        subscription = Subscription.objects.get(organization_id=graph.organization_a)
        change_plan(
            clinic_id=graph.clinic_a,
            plan_code=plan.code,
            expected_revision=subscription.revision,
            command_id=uuid4(),
        )

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.subscriptions import services
from apps.subscriptions.models import Subscription, SubscriptionEvent

from identity.permission_support import owner_context, permission_context
from subscriptions.test_contracts import administrator

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_committed_revocation_between_scope_and_use_is_a_silent_denial(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    actor = administrator(graph)
    original = services._scope

    def active(*, value: bool) -> None:
        with psycopg.connect(superuser_database_url) as administrative:
            administrative.execute("SET LOCAL ROLE clinic_owner")
            administrative.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [value, actor],
            )

    def revoke_after_real_scope(clinic_id: UUID, *, manage: bool = False) -> UUID:
        organization = original(clinic_id, manage=manage)
        active(value=False)
        return organization

    for operation in ("read", "change"):
        active(value=True)
        with monkeypatch.context() as patch:
            patch.setattr(services, "_scope", revoke_after_real_scope)
            with permission_context(graph, actor):
                if operation == "read":
                    with pytest.raises(services.SubscriptionDeniedError):
                        services.subscription_snapshot(clinic_id=graph.clinic_a)
                else:
                    with pytest.raises(services.SubscriptionDeniedError):
                        services.change_plan(
                            clinic_id=graph.clinic_a,
                            plan_code="core_operations",
                            expected_revision=1,
                            command_id=uuid4(),
                        )
        with owner_context(graph.organization_a):
            assert (
                Subscription.objects.get(organization_id=graph.organization_a).revision
                == 1
            )
            assert (
                SubscriptionEvent.objects.filter(
                    organization_id=graph.organization_a
                ).count()
                == 1
            )

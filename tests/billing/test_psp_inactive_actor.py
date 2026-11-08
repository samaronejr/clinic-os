"""PSP SQL rechecks actor activity without Python or tenant-entry admission."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from apps.billing.psp_models import PspBinding, PspEvidence, PspIntent
from apps.identity.models import User
from django.db import DatabaseError, connection, transaction

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_review import prepared_payment
from identity.permission_support import owner_context, permission_context

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "boundary", ["access", "dispatch-fresh", "dispatch-recovery", "binding", "evidence"]
)
def test_psp_sql_requires_current_active_actor(
    prepared_payment: tuple[FinanceSetup, PspIntent], boundary: str
) -> None:
    setup, intent = prepared_payment
    actor = intent.requested_by_id
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": setup.graph.organization_a,
        "intent_id": intent.pk,
        "provider": intent.provider,
    }
    model: type[PspBinding | PspEvidence] = PspBinding
    if boundary == "evidence":
        with finance_scope(setup):
            PspBinding.objects.create(
                organization_id=setup.graph.organization_a,
                intent_id=intent.pk,
                provider=intent.provider,
                provider_reference="synthetic-inactive-actor",
            )
        model = PspEvidence
        values.update(
            event_id="synthetic-inactive-evidence",
            source="status",
            status="pending",
            decision="recorded",
            amount_minor=intent.amount_minor,
            currency=intent.currency,
            version=1,
        )
    else:
        values["provider_reference"] = "synthetic-inactive-actor"
    decisions = []
    for active in (True, False, True):
        with owner_context(setup.graph.organization_a):
            assert User.objects.filter(pk=actor).update(is_active=active) == 1
        # Bind GUCs directly as clinic_app: tenant_context would pre-reject the
        # inactive actor and could hide a missing SQL check.
        with permission_context(setup.graph, actor), connection.cursor() as cursor:
            if boundary == "access":
                cursor.execute("SELECT clinic_app.billing_psp_access(%s)", [intent.pk])
                accepted = cursor.fetchone() == (True,)
                assert accepted is active
                decisions.append(accepted)
            elif boundary.startswith("dispatch-"):
                cursor.execute(
                    "SELECT clinic_app.billing_psp_dispatchable(%s,%s)",
                    [intent.pk, boundary == "dispatch-fresh"],
                )
                accepted = cursor.fetchone() == (True,)
                assert accepted is active
                decisions.append(accepted)
            elif active:
                row = model.objects.create(**values)
                assert model.objects.filter(pk=row.pk).exists()
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    model.objects.create(**values)
                cause = denied.value.__cause__
                assert isinstance(cause, psycopg.Error)
                assert cause.sqlstate == "42501"
                assert "billing_psp_guard(" in (cause.diag.context or "")
                decisions.append(False)
            # Keep the same command, row identity and economic state in every
            # phase. Only the persisted user flag changes between SQL attempts.
            transaction.set_rollback(True)
    assert decisions == [True, False, True]

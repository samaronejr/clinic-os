"""Insurance SQL rejects inactive actors without Python or tenant-entry gates."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import User
from apps.insurance import services
from apps.insurance.models import Appeal, Payer
from django.db import DatabaseError, transaction

from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from billing.journal_support import FinanceSetup
    from insurance.cycle_support import Cycle

pytestmark = pytest.mark.django_db(transaction=True)

ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.insurance_guard()": (
        "insurance.test_sql_inactive_actor."
        "test_insurance_guard_rechecks_the_current_active_actor",
        "insurance.test_sql_inactive_actor."
        "test_retained_approval_rechecks_the_original_clinicians_active_state",
    )
}


def _guard_denial(error: DatabaseError, state: str) -> None:
    cause = error.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.sqlstate == state
    assert "insurance_guard(" in (cause.diag.context or "")


def test_insurance_guard_rechecks_the_current_active_actor(
    finance_setup: FinanceSetup,
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    actor = graph.clinic_admin
    values = {
        "id": uuid4(),
        "organization_id": graph.organization_a,
        "clinic_id": setup.clinic_id,
        "command_id": uuid4(),
        "fingerprint": "a" * 64,
        "ans_registry": "123456",
        "name": "Synthetic native actor oracle",
    }
    decisions = []
    for active in (True, False, True):
        with owner_context(graph.organization_a):
            assert User.objects.filter(pk=actor).update(is_active=active) == 1
        with permission_context(graph, actor):
            if active:
                payer = Payer.objects.create(**values)
                assert Payer.objects.filter(pk=payer.pk).exists()
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    Payer.objects.create(**values)
                _guard_denial(denied.value, "42501")
                decisions.append(False)
            transaction.set_rollback(True)
    assert decisions == [True, False, True]


def test_retained_approval_rechecks_the_original_clinicians_active_state(
    cycle: Cycle,
) -> None:
    graph = cycle.finance.graph
    with permission_context(graph, graph.physician):
        approval = services.approve_appeal_assertions(
            clinic_id=cycle.finance.clinic_id,
            appeal_id=cycle.appeal_id,
            command_id=uuid4(),
        )
        assert approval.approved_by_id == graph.physician
    decisions = []
    for active in (True, False, True):
        with owner_context(graph.organization_a):
            changed = User.objects.filter(pk=graph.physician).update(is_active=active)
            assert changed == 1
        with permission_context(graph, graph.clinic_admin):
            if active:
                changed = Appeal.objects.filter(pk=cycle.appeal_id).update(
                    state="approved"
                )
                assert changed == 1
                assert Appeal.objects.get(pk=cycle.appeal_id).state == "approved"
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    Appeal.objects.filter(pk=cycle.appeal_id).update(state="approved")
                _guard_denial(denied.value, "23514")
                decisions.append(False)
            transaction.set_rollback(True)
    assert decisions == [True, False, True]

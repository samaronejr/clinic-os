"""Fiscal SQL refuses inactive actors before creating attributed history."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.billing.models import FiscalDocument, FiscalTransition
from apps.identity.models import User
from django.db import DatabaseError, connection, transaction

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_fiscal_database import draft
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ("finance_setup",)
pytestmark = pytest.mark.django_db(transaction=True)


def _active(setup: FinanceSetup, actor: UUID, *, active: bool) -> None:
    with owner_context(setup.graph.organization_a):
        assert User.objects.filter(pk=actor).update(is_active=active) == 1


def test_fiscal_access_requires_current_active_actor(
    finance_setup: FinanceSetup,
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    decisions = []
    for active in (True, False, True):
        _active(setup, graph.clinic_admin, active=active)
        with (
            permission_context(graph, graph.clinic_admin),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.billing_fiscal_access(%s)", [setup.clinic_id]
            )
            decisions.append(cursor.fetchone()[0])
    assert decisions == [True, False, True]


def test_fiscal_native_insert_requires_current_active_actor(
    finance_setup: FinanceSetup,
) -> None:
    setup, graph = finance_setup, finance_setup.graph
    document = draft(setup)
    with finance_scope(setup):
        values = {
            field.attname: getattr(document, field.attname)
            for field in FiscalDocument._meta.concrete_fields
        }
    command = uuid4()
    values.update(id=uuid4(), create_command_id=command, last_command_id=command)
    decisions = []
    for active in (True, False, True):
        _active(setup, graph.clinic_admin, active=active)
        with permission_context(graph, graph.clinic_admin):
            if active:
                created = FiscalDocument.objects.create(**values)
                transition = FiscalTransition.objects.get(
                    document=created, command_id=command
                )
                assert transition.actor_id == graph.clinic_admin
                assert transition.to_status == "draft"
                decisions.append(True)
            else:
                with pytest.raises(DatabaseError) as denied, transaction.atomic():
                    FiscalDocument.objects.create(**values)
                cause = denied.value.__cause__
                assert isinstance(cause, psycopg.Error)
                assert cause.sqlstate == "42501"
                assert "billing_fiscal_guard(" in (cause.diag.context or "")
                decisions.append(False)
            transaction.set_rollback(True)
    assert decisions == [True, False, True]
    with owner_context(graph.organization_a):
        assert not FiscalTransition.objects.filter(command_id=command).exists()

"""CRM all-role, current-permission and raw SQL protocol witnesses."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.comms.inbox_services import (
    create_template_version,
    open_conversation,
    set_template_approval,
)
from apps.comms.models import Message
from apps.crm.access import CrmAccessDeniedError, manager
from apps.identity.models import RoleGrant, User, UserClinicRole
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from auth.stepup_test_support import create_role_actor
from crm.support import world
from crm.test_campaigns import _opt_in
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _decision(actor: UUID, organization: UUID, clinic: UUID) -> tuple[bool, bool]:
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for name, value in (
            ("app.current_user_id", actor),
            ("app.current_tenant", organization),
        ):
            cursor.execute("SELECT set_config(%s,%s,true)", [name, str(value)])
        try:
            manager(clinic)
        except CrmAccessDeniedError:
            python_allowed = False
        else:
            python_allowed = True
        cursor.execute("SELECT clinic_app.crm_manager(%s)", [clinic])
        row = cursor.fetchone()
        assert row is not None
        return python_allowed, row[0]


def test_manager_role_permissions_remain_current(rbac_graph: RbacGraph) -> None:
    """Check every canonical role, wrong scope, inactivity and permission removal."""
    actor = None
    for role in UserClinicRole.Role:
        user = create_role_actor(rbac_graph, role)
        expected = role == UserClinicRole.Role.CLINIC_MANAGER
        assert _decision(user.pk, rbac_graph.organization_a, rbac_graph.clinic_a) == (
            expected,
            expected,
        )
        assert _decision(user.pk, rbac_graph.organization_a, rbac_graph.clinic_b) == (
            False,
            False,
        )
        assert _decision(user.pk, rbac_graph.organization_b, rbac_graph.clinic_a) == (
            False,
            False,
        )
        if expected:
            actor = user
    assert actor is not None
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=actor.pk).update(is_active=False)
    assert _decision(actor.pk, rbac_graph.organization_a, rbac_graph.clinic_a) == (
        False,
        False,
    )
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=actor.pk).update(is_active=True)
    assert _decision(actor.pk, rbac_graph.organization_a, rbac_graph.clinic_a) == (
        True,
        True,
    )
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role=UserClinicRole.Role.CLINIC_MANAGER,
            permission="demographics.read",
            valid_from=timezone.now() - timedelta(days=1),
        )
    assert _decision(actor.pk, rbac_graph.organization_a, rbac_graph.clinic_a) == (
        False,
        False,
    )


def _raw_lead_refusal(actor: UUID, organization: UUID, clinic: UUID) -> tuple[str, str]:
    """Insert one unsourced lead as clinic_app, bypassing Python admission."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for name, value in (
            ("app.current_user_id", actor),
            ("app.current_tenant", organization),
        ):
            cursor.execute("SELECT set_config(%s,%s,true)", [name, str(value)])
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            cursor.execute(
                "INSERT INTO clinic_app.crm_lead "
                "(id, organization_id, clinic_id, source_id) VALUES (%s,%s,%s,%s)",
                [uuid4(), organization, clinic, uuid4()],
            )
    cause = refused.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert "crm_guard()" in (cause.diag.context or "")
    assert cause.sqlstate is not None
    assert cause.diag.message_primary is not None
    return cause.sqlstate, cause.diag.message_primary


def test_raw_crm_guard_refuses_an_inactive_manager(rbac_graph: RbacGraph) -> None:
    """Only is_active changes; the guard's own manager clause decides."""
    actor = create_role_actor(rbac_graph, UserClinicRole.Role.CLINIC_MANAGER)
    outcomes = []
    for active in (True, False, True):
        with owner_context(rbac_graph.organization_a):
            assert User.objects.filter(pk=actor.pk).update(is_active=active) == 1
        outcomes.append(
            _raw_lead_refusal(actor.pk, rbac_graph.organization_a, rbac_graph.clinic_a)
        )
    # An active manager passes the authority clause and reaches validation.
    assert outcomes == [
        ("23514", "invalid lead source"),
        ("42501", "CRM manager required"),
        ("23514", "invalid lead source"),
    ]


def test_immutable_sql_receipts_cannot_be_truncated(rbac_graph: RbacGraph) -> None:
    with (
        owner_context(rbac_graph.organization_a),
        connection.cursor() as cursor,
        pytest.raises(DatabaseError, match="immutable CRM receipt"),
    ):
        cursor.execute("TRUNCATE clinic_app.crm_marketingpreference")


def test_raw_marketing_template_rejected_by_sql(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    state = world(rbac_graph, settings)
    _opt_in(state)
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        template = create_template_version(
            clinic_id=state.graph.clinic_a,
            channel="email",
            name=f"unsafe_{uuid4().hex}",
            language="pt_BR",
            category="marketing",
            purpose="marketing",
            body="Your diagnosis is a synthetic negative control.",
        )
        for status in ("pending", "approved"):
            set_template_approval(
                clinic_id=state.graph.clinic_a,
                template_version_id=template,
                status=status,
            )
        opened = open_conversation(
            clinic_id=state.graph.clinic_a,
            enrollment_id=state.enrollment,
            channel="email",
            purpose="marketing",
        )
        with (
            transaction.atomic(),
            pytest.raises(DatabaseError, match="neutral marketing content required"),
        ):
            Message.objects.create(
                organization_id=state.graph.organization_a,
                conversation_id=opened.conversation_id,
                direction="out",
                body="Synthetic clinical content must not be sent.",
                template_id=template,
                template_parameters="[]",
                sent_by_id=state.manager,
                command_id=uuid4(),
                created_at=timezone.now(),
            )

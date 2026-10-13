"""Actual authority, strict callback results and read-only onboarding checks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from apps.identity import onboarding
from apps.identity.lifecycle_authority import LifecycleDeniedError
from apps.identity.models import OnboardingProgress
from apps.identity.onboarding import OnboardingCheckError, onboarding_progress
from apps.identity.onboarding_registry import OnboardingStep
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection
from django.utils import timezone

from identity.permission_support import owner_context
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _steps(
    monkeypatch: pytest.MonkeyPatch,
    check: Mock,
    *,
    permission: str | None = None,
) -> None:
    step = OnboardingStep("synthetic_callback", permission, check)
    monkeypatch.setattr(onboarding, "registered_onboarding_steps", lambda: (step,))


@pytest.mark.parametrize("completed", [False, True])
def test_boolean_completion_has_one_resumable_server_receipt(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    completed: bool,
) -> None:
    graph = rbac_graph
    check = Mock(return_value=completed)
    _steps(monkeypatch, check)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        first = onboarding_progress(clinic_id=graph.clinic_b, persist=True)
        replay = onboarding_progress(clinic_id=graph.clinic_b, persist=True)
        assert replay == first
        assert first[0].completed is completed
        assert (first[0].completed_at is not None) is completed
        assert OnboardingProgress.objects.filter(clinic_id=graph.clinic_b).count() == 1


@pytest.mark.parametrize("result", [1, "true", None])
def test_non_boolean_completion_cannot_become_a_persisted_grant(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    result: int | str | None,
) -> None:
    graph = rbac_graph
    _steps(monkeypatch, Mock(return_value=result))
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with pytest.raises(OnboardingCheckError):
            onboarding_progress(clinic_id=graph.clinic_b, persist=True)
        assert not OnboardingProgress.objects.exists()


def test_async_completion_is_closed_and_refused_without_a_receipt(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    _steps(monkeypatch, AsyncMock(return_value=True))
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with pytest.raises(OnboardingCheckError):
            onboarding_progress(clinic_id=graph.clinic_b, persist=True)
        assert not OnboardingProgress.objects.exists()


def test_callback_cannot_change_domain_state_or_leak_read_only_mode(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        receipt = OnboardingProgress.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            step_key="synthetic_callback",
        )

    def mutate(*, clinic_id: UUID) -> bool:
        OnboardingProgress.objects.filter(clinic_id=clinic_id).update(
            completed_at=timezone.now()
        )
        return True

    _steps(monkeypatch, Mock(side_effect=mutate))
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with pytest.raises(DatabaseError) as failure:
            onboarding_progress(clinic_id=graph.clinic_b, persist=True)
        assert getattr(failure.value.__cause__, "sqlstate", None) == "25006"
        receipt.refresh_from_db()
        assert receipt.completed_at is None
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone() == ("off",)


def test_permission_filtered_callback_is_not_invoked(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    check = Mock(return_value=True)
    _steps(monkeypatch, check, permission="staff.organization")
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        assert onboarding_progress(clinic_id=graph.clinic_b, persist=True) == ()
        check.assert_not_called()
        assert not OnboardingProgress.objects.exists()


def test_foreign_clinic_is_refused_before_callback_invocation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    check = Mock(return_value=True)
    _steps(monkeypatch, check)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        with pytest.raises(LifecycleDeniedError):
            onboarding_progress(clinic_id=graph.clinic_c, persist=True)
        check.assert_not_called()
        assert not OnboardingProgress.objects.exists()

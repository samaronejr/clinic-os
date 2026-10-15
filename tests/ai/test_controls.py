"""Organization authority, owner commands, and real native refusal exits."""

from __future__ import annotations

import io
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import views
from apps.ai.controls import current_policy, set_organization_policy
from apps.ai.models import GatewaySwitch
from apps.ai.owner import set_global_switch
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.current_context import CurrentActorError
from apps.identity.management.base import LifecycleCommandError
from apps.identity.models import RoleGrant, UserClinicRole
from apps.tenancy.db import tenant_context
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from ai.support import policy, seed
from auth.stepup_test_support import create_role_actor, verified_request
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest

    from ai.support import GatewayWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def native_request(
    actor: UUID, clinic: UUID, *, data: dict[str, str] | None = None
) -> HttpRequest:
    authenticated = verified_request(actor)
    path = reverse("ai:settings", args=(clinic,))
    request = (
        RequestFactory().post(path, data)
        if data is not None
        else RequestFactory().get(path)
    )
    request.user = authenticated.user
    request.session = authenticated.session
    request.resolver_match = resolve(path)
    request.session.modified = False
    return request


def submission(world: GatewayWorld) -> dict[str, str]:
    return {
        "expected_version": "2",
        "enabled": "on",
        "purposes": "front_desk",
        "monthly_budget_micros": "100000",
        "limit_front_desk": "100000",
    }


def test_owner_cli_changes_only_synthetic_model_and_global_controls(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a current synthetic configuration and the real owner connection.
    seed(rbac_graph, monkeypatch)
    output = io.StringIO()
    # When: the CLI changes the global stop without a provider activation.
    call_command("ai_gateway", "global-switch", "--disable", stdout=output)
    # Then: only the model gateway is disabled; runtime cannot write that row.
    assert GatewaySwitch.objects.get(pk=1).enabled is False
    with runtime_role(), pytest.raises(LifecycleCommandError):
        set_global_switch(enabled=True, approved_regions=("br-synthetic",))


def test_native_controls_execute_valid_invalid_stale_and_forbidden_requests(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: current org-wide authority and real OTP-backed request objects.
    world = seed(rbac_graph, monkeypatch)
    g = rbac_graph
    valid = submission(world)
    requests: list[tuple[UUID, UUID, dict[str, str] | None, int]] = [
        (world.administrator, g.clinic_a, None, 200),
        (
            world.administrator,
            g.clinic_a,
            {**valid, "region": "US"},
            400,
        ),
        (
            world.administrator,
            g.clinic_a,
            {**valid, "monthly_budget_micros": "-1"},
            400,
        ),
        (
            world.administrator,
            g.clinic_a,
            {**valid, "expected_version": "1"},
            409,
        ),
        (world.administrator, g.clinic_a, valid, 302),
        (g.shared_user, g.clinic_a, None, 403),
        (world.administrator, g.clinic_c, None, 403),
    ]
    # When: every native command traverses the real view and service.
    for actor, clinic, data, expected in requests:
        request = native_request(actor, clinic, data=data)
        match = request.resolver_match
        assert match is not None
        assert match.kwargs["clinic_id"] == clinic
        # OTPMiddleware's lazy device lookup needs the known actor's GUC.
        with runtime_role(), tenant_context(actor, g.organization_a):
            response = views.settings_view(request, clinic)
        # Then: refusals never modify a session or write a session cookie.
        assert response.status_code == expected
        if expected in (403, 404):
            assert request.session.modified is False
            assert not response.cookies
    with runtime_role(), tenant_context(world.administrator, g.organization_a):
        assert current_policy(clinic_id=g.clinic_a).monthly_budget_micros == 100_000
        snapshot = latest_configuration(g.clinic_a)
        assert snapshot is not None
        assert snapshot.version == 3


def test_ai_policy_requires_organization_permission_in_every_clinic(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: all canonical stored roles, each scoped to only clinic A.
    world = seed(rbac_graph, monkeypatch)
    for role in UserClinicRole.Role:
        actor = create_role_actor(rbac_graph, role)
        # When/Then: no single-clinic assignment can change organization AI.
        with (
            runtime_role(),
            tenant_context(actor.pk, rbac_graph.organization_a),
            pytest.raises(CurrentActorError),
        ):
            set_organization_policy(
                clinic_id=rbac_graph.clinic_a,
                expected_version=2,
                policy=policy(enabled=False),
            )
    # A remove-only grant revokes the otherwise-authorized owner's command.
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            role="owner",
            bundle_version=1,
            permission="configuration.organization",
            effect="remove",
            valid_from=timezone.now(),
        )
    with (
        runtime_role(),
        tenant_context(world.administrator, rbac_graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        set_organization_policy(
            clinic_id=rbac_graph.clinic_a,
            expected_version=2,
            policy=replace(policy(), enabled=False),
        )


def test_unknown_and_foreign_clinic_control_reads_refuse_equally(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: current owner authority in this organization only.
    world = seed(rbac_graph, monkeypatch)
    # When/Then: neither a foreign clinic nor an unknown selector is revealed.
    with runtime_role(), tenant_context(world.administrator, rbac_graph.organization_a):
        for clinic in (rbac_graph.clinic_c, uuid4()):
            with pytest.raises(CurrentActorError):
                current_policy(clinic_id=clinic)


def test_org_admin_can_change_only_ai_fields_with_all_clinics_in_scope(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: org_admin authority in both clinics, without a legacy admin role.
    seed(rbac_graph, monkeypatch)
    actor = create_role_actor(rbac_graph, UserClinicRole.Role.ORG_ADMIN)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user=actor,
            role="org_admin",
        )
    # When: the org admin publishes an AI-only snapshot.
    with runtime_role(), tenant_context(actor.pk, rbac_graph.organization_a):
        changed = set_organization_policy(
            clinic_id=rbac_graph.clinic_a,
            expected_version=2,
            policy=policy(enabled=False),
        )
        assert changed.ai_policy["enabled"] is False
        # Then: adding an unrelated branding mutation still fails in PostgreSQL.
        changed.id = uuid4()
        changed.version += 1
        changed.display_name = "Unauthorized branding change"
        with pytest.raises(DatabaseError), transaction.atomic():
            changed.save(force_insert=True)

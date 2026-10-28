"""Observe decision-support boundaries with valid subjects and current roles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ai import tasks
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.decision_policy import authorize_decision, authorized_catalog
from apps.ai.decision_support import display_decision_support, request_decision_support
from apps.ai.decision_views import decision_support
from apps.ai.models import AIInvocation
from apps.ai.owner import set_global_switch
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from django.test import RequestFactory
from django.urls import resolve, reverse

from ai.brief_support import BINDING, BriefWorld
from ai.decision_support_helpers import enable, finish, generate
from ai.support import GatewayWorld, model
from auth.stepup_test_support import verified_request
from ehr.longitudinal_support import ChartWorld
from ehr.test_autosave import as_actor, registration
from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    import pytest

    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph


def decision_calls(world: BriefWorld, actor: UUID) -> dict[str, Callable[[], bool]]:
    """Keep input, output, patient disclosure and gate records real and valid."""
    queued = generate(world)
    assert finish(world, queued) == "succeeded"
    assert queued.invocation_id is not None
    g = world.graph
    encounter = world.chart.version.document.encounter_id
    with as_actor(g, g.physician):
        content = AIInvocation.objects.get(pk=queued.invocation_id).inputs_ref.content
    path = reverse("ai:decision-support", args=(g.clinic_a,))
    authentication = verified_request(actor)
    authentication.session.save()
    request = RequestFactory().post(
        path, {"command": "open", "encounter_id": str(encounter)}
    )
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)
    invocation_id = queued.invocation_id
    return {
        "apps.ai.decision_policy.authorize_decision": lambda: (
            authorize_decision(
                clinic_id=g.clinic_a,
                content=content,
                enrollment_id=world.chart.enrollment,
            ).encounter
            == encounter
        ),
        "apps.ai.decision_policy.authorized_catalog": lambda: bool(
            authorized_catalog(clinic_id=g.clinic_a)
        ),
        "apps.ai.decision_support.request_decision_support": lambda: (
            request_decision_support(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                session_binding=BINDING,
                opt_in=True,
            ).invocation_id
            is not None
        ),
        "apps.ai.decision_support.display_decision_support": lambda: (
            display_decision_support(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                session_binding=BINDING,
                invocation_id=invocation_id,
            ).state
            == "ready"
        ),
        "apps.ai.decision_views.decision_support": lambda: (
            decision_support(request, g.clinic_a).status_code == 200
        ),
    }


def decision_probe(symbol: str, invoke: Callable[[], bool]) -> DifferentialProbe:
    """Translate only declared access refusals; unexpected failures stay failures."""

    def call() -> bool:
        try:
            return invoke()
        except (
            CurrentActorError,
            ClinicalAccessDeniedError,
            GatewayAccessDeniedError,
            GatewayRefusedError,
        ):
            return False

    return DifferentialProbe(symbol, call, expected=False)


def census_world(
    data: NonstaffSubjects, graph: RbacGraph, patch: pytest.MonkeyPatch
) -> BriefWorld:
    """Reuse the census encounter instead of repeating scheduling or role seeds."""
    patch.setattr(tasks.execute, "apply_async", lambda **_kwargs: None)
    patch.setattr(tasks, "fair_acquire", lambda **_kwargs: True)
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-decision-census-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    configuration = model(purpose=Purpose.DECISION_SUPPORT)
    administrator = User.objects.create(
        username="synthetic-decision-census-owner-" + uuid4().hex
    )
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user=administrator,
                role="owner",
            )
        UserClinicRole.objects.filter(
            clinic_id=graph.clinic_a, user_id=graph.physician
        ).update(workflow_bundle_version=2)
    registration(graph, graph.physician, graph.clinic_a)
    with as_actor(graph, administrator.pk):
        if latest_configuration(graph.clinic_a) is None:
            publish_configuration(
                clinic_id=graph.clinic_a,
                expected_version=0,
                content=ConfigurationContent("Synthetic decision census"),
            )
    world = BriefWorld(
        graph,
        GatewayWorld(graph, administrator.pk, configuration, []),
        ChartWorld(
            data.legacy.version,
            data.op.enrollment,
            data.legacy.appointment.patient_id,
        ),
    )
    enable(world)
    return world


def decision_probes(
    data: NonstaffSubjects, graph: RbacGraph, patch: pytest.MonkeyPatch
) -> list[DifferentialProbe]:
    """Run the valid-subject deny controls through the primary authority observer."""
    calls = decision_calls(census_world(data, graph, patch), data.actor.pk)
    return [decision_probe(symbol, invoke) for symbol, invoke in sorted(calls.items())]

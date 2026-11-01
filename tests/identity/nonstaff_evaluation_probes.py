"""Real evaluation allow/deny controls on the census actor and clinic."""

from __future__ import annotations

import io
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from apps.ai.artifacts import create_input
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.evaluation import services, views
from apps.ai.owner import set_global_switch, transition_model
from apps.ai.services import invoke
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import timezone

from ai.evaluation_support import as_reviewer, complete_reviews, evaluation_world
from ai.support import DEFAULT_BUDGET, policy
from auth.stepup_test_support import verified_request
from identity.delegation_probes import _as_owner
from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.ai.evaluation.services.authorized_run",
        "apps.ai.evaluation.services.decide_change",
        "apps.ai.evaluation.services.evaluation_staff",
        "apps.ai.evaluation.services.finish_run",
        "apps.ai.evaluation.services.review_slot",
        "apps.ai.evaluation.services.start_run",
        "apps.ai.evaluation.services.store_results",
        "apps.ai.evaluation.views._review",
        "apps.ai.evaluation.views._workspace",
        "apps.ai.evaluation.views.evaluation_view",
        "apps.ai.management.commands.ai_eval.Command.handle",
        "apps.ai.policy.check_configuration",
    }
)


def _memberships(graph: RbacGraph, actor: UUID, roles: tuple[str, ...]) -> None:
    """Trusted preparation precedes observation and rolls back with its scenario."""
    with _as_owner(), owner_context(graph.organization_a):
        for role in roles:
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=actor,
                role=role,
            )


def _request(
    actor: UUID, clinic: UUID, data: dict[str, str] | None = None
) -> HttpRequest:
    path = reverse("ai:evaluation", args=(clinic,))
    with _as_owner():
        authentication = verified_request(actor)
    request = (
        RequestFactory().get(path)
        if data is None
        else RequestFactory().post(path, data)
    )
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)
    return request


def _probe(
    symbol: str,
    invoke: Callable[[], object],
    *,
    allowed: bool,
    prepare: Callable[[], object] | None = None,
    database_role: str = "clinic_app",
) -> DifferentialProbe:
    def call() -> object:
        try:
            return invoke()
        except (CurrentActorError, GatewayAccessDeniedError, GatewayRefusedError):
            return False

    return DifferentialProbe(
        symbol, call, expected=allowed, prepare=prepare, database_role=database_role
    )


def _admission_probes(
    graph: RbacGraph,
    actor: UUID,
    *,
    approved_clinic: UUID,
    configuration_id: UUID,
) -> list[DifferentialProbe]:
    """Observe actual admission with only one clinic's evaluation approved."""
    input_refs: dict[UUID, UUID] = {}

    def prepare_admission(clinic: UUID) -> None:
        _memberships(graph, actor, ("owner",))
        with _as_owner(), owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_b,
                user_id=actor,
                role="owner",
            )
        current = latest_configuration(approved_clinic)
        if current is None:
            current = publish_configuration(
                clinic_id=approved_clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic evaluation census"),
            )
        set_organization_policy(
            clinic_id=approved_clinic,
            expected_version=current.version,
            policy=policy(),
        )
        input_refs[clinic] = create_input(
            clinic_id=clinic,
            purpose=Purpose.FRONT_DESK,
            content="Synthetic evaluation admission",
        ).pk

    def admit(clinic: UUID) -> bool:
        observed: list[tuple[object, ...]] = []

        def spy(
            execute: Callable[..., object],
            sql: str,
            params: Sequence[object] | None,
            many: bool,
            context: dict[str, object],
        ) -> object:
            if "clinic_app.ai_eval_activation_allowed(" in sql and params:
                observed.append(tuple(params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(spy):
            outcome = invoke(
                clinic_id=clinic,
                purpose=Purpose.FRONT_DESK,
                principal=actor,
                inputs_ref=input_refs[clinic],
                budget=DEFAULT_BUDGET,
            )
        assert observed == [(configuration_id, clinic)]
        assert outcome.reason in {None, "configuration_unavailable"}
        return outcome.invocation_id is not None

    return [
        _probe(
            "apps.ai.policy.check_configuration",
            partial(admit, clinic),
            allowed=allowed,
            prepare=partial(prepare_admission, clinic),
        )
        for allowed, clinic in ((True, approved_clinic), (False, graph.clinic_b))
    ]


def evaluation_probes(graph: RbacGraph, actor: UUID) -> list[DifferentialProbe]:
    """Keep records valid; vary real membership, assignment and clinic approvals."""
    world = evaluation_world(graph)
    assert complete_reviews(world).status == "passed"
    with as_reviewer(world, world.owner):
        change = services.request_change(clinic_id=world.clinic, run_id=world.run.pk)
        approval = services.request_change(clinic_id=world.clinic, run_id=world.run.pk)
    with as_reviewer(world, world.approver):
        services.decide_change(
            clinic_id=world.clinic,
            change_id=approval.pk,
            decision="approved",
            approval_ref="synthetic-census-clinic-approval",
        )
    configuration = transition_model(
        configuration_id=world.configuration.pk, state="active"
    )
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-evaluation-census-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    request = _request(actor, world.clinic)
    run = world.run
    review_request = _request(
        actor, world.clinic, {"action": "open", "run_id": str(run.pk)}
    )
    results = cast("dict[str, object]", world.run.sample_results)

    def start() -> object:
        return services.start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )

    def prepare_store(allowed: bool) -> None:
        nonlocal run
        _memberships(graph, actor, ("physician", "owner"))
        run = services.start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )
        if not allowed:
            with _as_owner(), owner_context(world.organization):
                UserClinicRole.objects.filter(user_id=actor).update(
                    revoked_at=timezone.now()
                )

    def prepare_review(allowed: bool, *, owner_on_denial: bool = False) -> None:
        nonlocal run, review_request
        run = world.run
        if allowed:
            _memberships(graph, actor, ("physician",))
            with _as_owner(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_user_id',%s,true)",
                    [str(world.owner)],
                )
                try:
                    run = services.start_run(
                        clinic_id=world.clinic,
                        protocol_id=world.protocol.pk,
                        configuration_id=world.configuration.pk,
                        reviewers=replace(world.reviewers, rater_one=actor),
                    )
                finally:
                    cursor.execute(
                        "SELECT set_config('app.current_user_id',%s,true)", [str(actor)]
                    )
        elif owner_on_denial:
            _memberships(graph, actor, ("owner",))
        review_request = _request(
            actor, world.clinic, {"action": "open", "run_id": str(run.pk)}
        )

    calls: dict[str, tuple[Callable[[], object], tuple[str, ...]]] = {
        "apps.ai.evaluation.services.authorized_run": (
            lambda: services.authorized_run(
                clinic_id=world.clinic, run_id=world.run.pk
            ),
            ("owner",),
        ),
        "apps.ai.evaluation.services.decide_change": (
            lambda: services.decide_change(
                clinic_id=world.clinic,
                change_id=change.pk,
                decision="approved",
                approval_ref="synthetic-census-independent-owner",
            ),
            ("owner",),
        ),
        "apps.ai.evaluation.services.evaluation_staff": (
            lambda: services.evaluation_staff(
                clinic_id=world.clinic, mode="clinical_owner"
            ),
            ("physician", "owner"),
        ),
        "apps.ai.evaluation.services.finish_run": (
            lambda: services.finish_run(clinic_id=world.clinic, run_id=world.run.pk),
            ("physician", "owner"),
        ),
        "apps.ai.evaluation.services.start_run": (start, ("physician", "owner")),
        "apps.ai.evaluation.views._workspace": (
            lambda: views._workspace(request, world.clinic).status_code == 200,
            ("owner",),
        ),
        "apps.ai.evaluation.views.evaluation_view": (
            lambda: views.evaluation_view(request, world.clinic).status_code == 200,
            ("owner",),
        ),
        "apps.ai.management.commands.ai_eval.Command.handle": (
            lambda: (
                call_command(
                    "ai_eval",
                    "finish",
                    "--organization-id",
                    str(world.organization),
                    "--clinic-id",
                    str(world.clinic),
                    "--operator-id",
                    str(actor),
                    "--run-id",
                    str(world.run.pk),
                    stdout=io.StringIO(),
                )
                == "passed"
            ),
            ("physician", "owner"),
        ),
    }
    probes = [
        _probe(
            symbol,
            invoke,
            allowed=allowed,
            prepare=partial(_memberships, graph, actor, roles) if allowed else None,
            database_role=(
                "clinic_owner"
                if symbol == "apps.ai.management.commands.ai_eval.Command.handle"
                else "clinic_app"
            ),
        )
        for symbol, (invoke, roles) in calls.items()
        for allowed in (True, False)
    ]
    for allowed in (True, False):
        probes.extend(
            [
                _probe(
                    "apps.ai.evaluation.services.store_results",
                    lambda: services.store_results(
                        clinic_id=world.clinic, run_id=run.pk, results=results
                    ),
                    allowed=allowed,
                    prepare=partial(prepare_store, allowed),
                ),
                _probe(
                    "apps.ai.evaluation.services.review_slot",
                    # Enter the real consumer: the unassigned owner can read the
                    # run, but must reach and fail the slot selector itself.
                    lambda: (
                        views._review(review_request, world.clinic, {}).status_code
                        == 200
                    ),
                    allowed=allowed,
                    prepare=partial(prepare_review, allowed, owner_on_denial=True),
                ),
                _probe(
                    "apps.ai.evaluation.views._review",
                    lambda: (
                        views._review(review_request, world.clinic, {}).status_code
                        == 200
                    ),
                    allowed=allowed,
                    prepare=partial(prepare_review, allowed),
                ),
            ]
        )
    probes.extend(
        _admission_probes(
            graph,
            actor,
            approved_clinic=world.clinic,
            configuration_id=configuration.pk,
        )
    )
    assert {probe.symbol for probe in probes} == SYMBOLS
    return probes

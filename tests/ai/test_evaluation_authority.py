"""Every stored role and raw inactive actor meets the deployed evaluation guards."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ai.evaluation.services import rate_case
from apps.identity import stepup
from apps.identity.models import User, UserClinicRole
from django.db import DatabaseError, connection, transaction

from ai.evaluation_support import as_reviewer, evaluation_world, no_errors
from auth.stepup_test_support import STEP_UP_NOW
from identity.nonstaff_differential import (
    DifferentialProbe,
    assert_behavioral_classifications,
)
from identity.nonstaff_evaluation_probes import SYMBOLS, evaluation_probes
from identity.nonstaff_states import ReplayScope
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def raw_actor(actor: UUID, organization: UUID) -> Iterator[None]:
    """Reach SQL itself without tenant_context's earlier inactive-actor refusal."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true),"
            "set_config('app.current_tenant',%s,true),"
            "set_config('app.current_principal','',true),"
            "set_config('app.current_patient_session','',true)",
            [str(actor), str(organization)],
        )
        yield


def test_evaluation_sql_checks_every_stored_role_and_exact_clinic(
    rbac_graph: RbacGraph,
) -> None:
    for role in UserClinicRole.Role.values:
        user = User.objects.create(username="synthetic-evaluation-role-" + uuid4().hex)
        with owner_context(rbac_graph.organization_a):
            UserClinicRole.objects.create(
                user=user,
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                role=role,
                workflow_bundle_version=2,
            )
        for active in (True, False):
            User.objects.filter(pk=user.pk).update(is_active=active)
            with (
                raw_actor(user.pk, rbac_graph.organization_a),
                connection.cursor() as cursor,
            ):
                for clinic in (
                    rbac_graph.clinic_a,
                    rbac_graph.clinic_b,
                    rbac_graph.clinic_c,
                ):
                    cursor.execute(
                        "SELECT clinic_app.ai_eval_staff(%s,'clinician'),"
                        "clinic_app.ai_eval_staff(%s,'owner'),"
                        "clinic_app.ai_eval_staff(%s,'clinical_owner'),"
                        "clinic_app.ai_eval_staff(%s,'unknown')",
                        [clinic] * 4,
                    )
                    scoped = active and clinic == rbac_graph.clinic_a
                    assert cursor.fetchone() == (
                        scoped and role == "physician",
                        scoped and role == "owner",
                        False,
                        False,
                    )
    world = evaluation_world(rbac_graph)
    with raw_actor(world.owner, world.organization), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_eval_staff(%s,'clinical_owner')", [world.clinic]
        )
        assert cursor.fetchone() == (True,)


def test_evaluation_census_observes_real_allow_and_deny_for_every_candidate(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
    actor = User.objects.create(username="synthetic-evaluation-census-" + uuid4().hex)
    inventory = json.loads(
        (Path(__file__).parents[1] / "identity/legacy_guards.json").read_text()
    )
    candidates = [row for row in inventory["candidates"] if row["symbol"] in SYMBOLS]
    assert {row["symbol"] for row in candidates} == SYMBOLS
    probes: dict[str, list[DifferentialProbe]] = {}
    for probe in evaluation_probes(rbac_graph, actor.pk):
        probes.setdefault(probe.symbol, []).append(probe)
    assert all(
        {probe.expected for probe in controls} == {True, False}
        for controls in probes.values()
    )
    report = assert_behavioral_classifications(
        candidates,
        probes,
        actor=actor,
        scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
    )
    assert report.observations.keys() == SYMBOLS
    assert report.executed == frozenset(symbol + "#observed" for symbol in SYMBOLS)


def test_evaluation_raw_readers_and_rating_guard_refuse_inactive_or_foreign_actor(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    for user in (world.reviewers.rater_one, world.reviewers.rater_two):
        with as_reviewer(world, user):
            rate_case(
                clinic_id=world.clinic,
                run_id=world.run.pk,
                case_id="synthetic-accents",
                labels=no_errors(),
            )
    for user in (
        world.reviewers.rater_one,
        world.reviewers.rater_two,
        world.reviewers.adjudicator,
    ):
        # Active assigned raters can read the run; the adjudicator can now see
        # the first vote because both immutable independent ratings exist.
        with raw_actor(user, world.organization), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.ai_eval_run_visible(%s),"
                "clinic_app.ai_eval_rating_visible(%s,%s,%s)",
                [world.run.pk, world.run.pk, "synthetic-accents", user],
            )
            assert cursor.fetchone() == (True, True)
        User.objects.filter(pk=user).update(is_active=False)
        with raw_actor(user, world.organization), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.ai_eval_run_visible(%s),"
                "clinic_app.ai_eval_rating_visible(%s,%s,%s)",
                [world.run.pk, world.run.pk, "synthetic-accents", user],
            )
            assert cursor.fetchone() == (False, False)
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "INSERT INTO clinic_app.ai_evaluationrating "
                    "(id,organization_id,clinic_id,run_id,case_id,slot,user_id,"
                    "labels,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
                    "statement_timestamp())",
                    [
                        uuid4(),
                        world.organization,
                        world.clinic,
                        world.run.pk,
                        "synthetic-noise",
                        "rater_one",
                        user,
                        json.dumps(no_errors()),
                    ],
                )
        User.objects.filter(pk=user).update(is_active=True)
        with raw_actor(user, rbac_graph.organization_b), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.ai_eval_run_visible(%s),"
                "clinic_app.ai_eval_rating_visible(%s,%s,%s)",
                [world.run.pk, world.run.pk, "synthetic-accents", user],
            )
            assert cursor.fetchone() == (False, False)
    with (
        raw_actor(rbac_graph.physician, world.organization),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.ai_eval_run_visible(%s)", [world.run.pk])
        assert cursor.fetchone() == (False,)
    with as_reviewer(world, world.owner), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.ai_evaluationrating WHERE run_id=%s",
            [world.run.pk],
        )
        # Pending owner access does not pierce independent-review blinding.
        assert cursor.fetchone() == (0,)

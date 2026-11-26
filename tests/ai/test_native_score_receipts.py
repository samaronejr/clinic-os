"""Only the owner scorer can issue; runtime may spend that exact receipt once."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.evaluation.runner import score_run
from apps.ai.evaluation.services import start_run, store_results
from apps.ai.models import EvaluationRun
from apps.core.secrets import secret_store
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import scoped_lifecycle_gucs
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import IntegrityError, connection, transaction

from ai.evaluation_support import as_reviewer, candidate, evaluation_world, review_world

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def _receipt_refused() -> Iterator[None]:
    with pytest.raises(IntegrityError) as refusal, transaction.atomic():
        yield
    cause = refusal.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert (cause.sqlstate, cause.diag.message_primary) == (
        "23514",
        "invalid evaluation receipt",
    )


def test_runtime_cannot_insert_precommitted_evaluation_run(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    with as_reviewer(world, world.owner):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user,current_setting('app.current_user_id')::uuid,"
                "current_setting('app.current_tenant')::uuid"
            )
            assert cursor.fetchone() == ("clinic_app", world.owner, world.organization)
        pending = start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )
        attempted = uuid4()
        insert = """
            INSERT INTO clinic_app.ai_evaluationrun
                (id, organization_id, clinic_id, protocol_id, configuration_id,
                 configuration_version, versions, stage, rater_one_id, rater_two_id,
                 adjudicator_id, created_by_id, automatic_failures, status, created_at,
                 producer_commitment)
            SELECT %s, organization_id, clinic_id, protocol_id, configuration_id,
                   configuration_version, versions, stage, rater_one_id, rater_two_id,
                   adjudicator_id, created_by_id, automatic_failures, status,
                   created_at, %s FROM clinic_app.ai_evaluationrun WHERE id = %s
        """
        runs = EvaluationRun.objects.values_list("pk", flat=True)
        before = set(runs)
        assert attempted not in before
        with _receipt_refused(), connection.cursor() as cursor:
            cursor.execute(insert, [attempted, bytes(32), pending.pk])
        assert set(runs.all()) == before

        with connection.cursor() as cursor:
            cursor.execute(insert, [attempted, None, pending.pk])
        assert set(runs.all()) == before | {attempted}


def test_issued_pending_commitment_cannot_be_replaced(rbac_graph: RbacGraph) -> None:
    world = evaluation_world(rbac_graph)
    with as_reviewer(world, world.owner):
        fresh = start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )
        with pytest.raises(LifecycleCommandError):
            score_run(fresh)
    score_run(fresh)
    with transaction.atomic(), scoped_lifecycle_gucs(world.organization, world.owner):
        row = EvaluationRun.objects.filter(pk=fresh.pk)
        state = row.values_list("producer_commitment", "scored_at", "status")
        issued = state.get()
        assert issued[0] is not None
        assert bytes(issued[0]) != bytes(32)
        assert issued[1:] == (None, "pending")
        with _receipt_refused():
            row.update(producer_commitment=bytes(32))
        assert state.get() == issued


@pytest.mark.parametrize(
    "kind", ["missing", "forged", "replay", "cross_run", "input", "scorer", "output"]
)
def test_native_writer_refuses_nonissued_or_spent_score(
    rbac_graph: RbacGraph, kind: str
) -> None:
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=candidate(),
    )
    with as_reviewer(world, world.owner):
        fresh = start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )
    genuine = score_run(fresh)
    assert genuine["scorer_id"] == fresh.versions["schema"]
    assert isinstance(genuine["input_sha256"], str)
    assert len(bytes.fromhex(genuine["input_sha256"])) == 32
    submitted = deepcopy(genuine)
    prior = world.run.sample_results
    if kind == "missing":
        del submitted["receipt"]
    elif kind == "forged":
        submitted["receipt"] = "invalid-receipt"
    elif kind == "cross_run":
        submitted["receipt"] = prior["receipt"]
    elif kind in {"input", "scorer"}:
        submitted["input_sha256" if kind == "input" else "scorer_id"] = "0" * 64
    elif kind == "output":
        cases = submitted["cases"]
        assert isinstance(cases, list)
        # The JSON domain reply stays valid and all automatic counts agree.
        # Only the already-scored observation bytes have changed.
        cases[0]["output"] += " "
    with as_reviewer(world, world.owner):
        if kind == "replay":
            store_results(clinic_id=world.clinic, run_id=fresh.pk, results=genuine)

        def snapshot() -> tuple[object, ...]:
            return EvaluationRun.objects.values_list(
                "sample_results",
                "scored_at",
                "automatic_failures",
                "producer_commitment",
                "status",
            ).get(pk=fresh.pk)

        before = snapshot()
        packet = protect(
            purpose="ai.evaluation.samples.v1",
            plaintext=json.dumps(
                submitted,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
        with _receipt_refused(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.ai_eval_store(%s,%s,%s,%s)",
                [
                    world.clinic,
                    fresh.pk,
                    packet,
                    secret_store().get_secret(KEK_SECRET_NAME),
                ],
            )
        assert snapshot() == before
        if kind != "replay":
            assert before[0] is None
            stored = store_results(
                clinic_id=world.clinic, run_id=fresh.pk, results=genuine
            )
            assert stored.sample_results == genuine


def test_native_scorer_receipt_catalog_posture() -> None:
    """Only the table owner may issue; the runtime writer uses its old ACL."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolname::text,has_column_privilege(rolname,"
            "'clinic_app.ai_evaluationrun','producer_commitment','UPDATE') "
            "FROM pg_roles WHERE rolname IN "
            "('clinic_app','clinic_agent','clinic_owner','clinic_resolver') ORDER BY 1"
        )
        assert cursor.fetchall() == [
            ("clinic_agent", False),
            ("clinic_app", False),
            ("clinic_owner", True),
            ("clinic_resolver", False),
        ]
        cursor.execute(
            "SELECT proname,prosecdef,pg_get_userbyid(proowner),proconfig "
            "FROM pg_proc WHERE oid IN ("
            "'clinic_app.ai_eval_record_guard()'::regprocedure,"
            "'clinic_app.ai_eval_store(uuid,uuid,bytea,text)'::regprocedure) ORDER BY 1"
        )
        search_path = ["search_path=pg_catalog, clinic_app, pg_temp"]
        assert cursor.fetchall() == [
            ("ai_eval_record_guard", True, "clinic_resolver", search_path),
            ("ai_eval_store", True, "clinic_resolver", search_path),
        ]

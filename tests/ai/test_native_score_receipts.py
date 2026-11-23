"""Only the owner scorer can issue; runtime may spend that exact receipt once."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from apps.ai.evaluation.runner import score_run
from apps.ai.evaluation.services import start_run, store_results
from apps.ai.models import EvaluationRun
from apps.core.secrets import secret_store
from apps.identity.management.base import LifecycleCommandError
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import IntegrityError, connection, transaction

from ai.evaluation_support import as_reviewer, candidate, review_world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "kind", ["missing", "forged", "replay", "cross_run", "input", "scorer", "output"]
)
def test_native_writer_refuses_nonissued_or_spent_score(
    rbac_graph: RbacGraph, kind: str
) -> None:
    # Require a protected trust anchor before exercising negative inputs: the
    # rejected baseline has none. Never let a vulnerable writer persist one.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT attname FROM pg_attribute WHERE "
            "attrelid='clinic_app.ai_evaluationrun'::regclass "
            "AND attname='producer_commitment' AND NOT attisdropped"
        )
        assert cursor.fetchone() == ("producer_commitment",)
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=candidate(),
    )
    with as_reviewer(world, world.owner):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user,has_column_privilege(current_user,"
                "'clinic_app.ai_evaluationrun','producer_commitment','UPDATE')"
            )
            assert cursor.fetchone() == ("clinic_app", False)
        fresh = start_run(
            clinic_id=world.clinic,
            protocol_id=world.protocol.pk,
            configuration_id=world.configuration.pk,
            reviewers=world.reviewers,
        )
        with pytest.raises(LifecycleCommandError):
            score_run(fresh)
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
        with (
            pytest.raises(
                IntegrityError, match="invalid evaluation receipt"
            ) as refusal,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.ai_eval_store(%s,%s,%s,%s)",
                [
                    world.clinic,
                    fresh.pk,
                    packet,
                    secret_store().get_secret(KEK_SECRET_NAME),
                ],
            )
        assert getattr(refusal.value.__cause__, "sqlstate", None) == "23514"
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
            "SELECT p.prosecdef,pg_get_userbyid(p.proowner),"
            "strpos(p.prosrc,'r.producer_commitment IS DISTINCT FROM')>0,"
            "strpos(p.prosrc,'ai.evaluation.samples.v1')>0 "
            "FROM pg_proc p WHERE p.oid="
            "'clinic_app.ai_eval_store(uuid,uuid,bytea,text)'::regprocedure"
        )
        assert cursor.fetchone() == (True, "clinic_resolver", True, True)

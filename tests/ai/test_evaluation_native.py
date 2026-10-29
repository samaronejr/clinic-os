"""Native blinded forms exercise real role/RLS, required answers and immutable votes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.evaluation.corpus import load_corpus
from apps.ai.evaluation.protocol import ERROR_LABELS
from apps.ai.evaluation.services import finish_run, rate_case, request_change
from apps.ai.evaluation.views import evaluation_view
from apps.ai.models import EvaluationRating
from apps.identity import stepup
from apps.tenancy.db import tenant_context
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils.translation import gettext

from ai.evaluation_support import (
    EvaluationWorld,
    as_reviewer,
    complete_reviews,
    evaluation_world,
    no_errors,
    review_world,
)
from auth.stepup_test_support import STEP_UP_NOW, verified_request
from core.test_workspace_boundaries import _normalized_denial
from core.test_workspace_in_view_refusals import assert_session_read_only
from ehr.test_addenda import signed_in
from scribe.test_refusals import snapshot as committed_state

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def fixed_review_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


def native_review(
    world: EvaluationWorld,
    user: UUID,
    data: dict[str, str] | None,
    *,
    enhanced: bool = False,
) -> HttpResponse:
    path = reverse("ai:evaluation", args=(world.clinic,))
    authentication = verified_request(user)
    request = (
        RequestFactory().get(path)
        if data is None
        else RequestFactory().post(
            path, data, headers={"HX-Request": "true"} if enhanced else {}
        )
    )
    request.user = authentication.user
    request.session = authentication.session
    request.resolver_match = resolve(path)
    with as_reviewer(world, user):
        response = evaluation_view(request, world.clinic)
    assert isinstance(response, HttpResponse)
    return response


@pytest.mark.parametrize("enhanced", [False, True])
def test_blinded_native_form_validates_every_answer_and_keeps_one_vote(
    rbac_graph: RbacGraph, enhanced: bool
) -> None:
    world = evaluation_world(rbac_graph)
    user = world.reviewers.rater_one
    queue = native_review(world, user, None)
    assert queue.status_code == 200
    assert str(world.run.pk) in queue.content.decode()
    payload = {
        "action": "open",
        "run_id": str(world.run.pk),
        "case_id": "synthetic-accents",
    }
    opened = native_review(world, user, payload, enhanced=enhanced)
    html = opened.content.decode()
    assert opened.status_code == 200
    assert opened["Cache-Control"] == "private, no-store"
    assert "data-evaluation-rating" in html
    assert world.configuration.model_id not in html
    assert str(world.reviewers.rater_two) not in html
    invalid = native_review(
        world, user, dict(payload, action="rate"), enhanced=enhanced
    )
    assert invalid.status_code == 400
    with as_reviewer(world, user):
        assert not EvaluationRating.objects.filter(run=world.run).exists()
    answers = {key: str(value) for key, value in no_errors().items()}
    saved = native_review(
        world, user, {**payload, **answers, "action": "rate"}, enhanced=enhanced
    )
    assert saved.status_code == 200
    assert "data-evaluation-saved" in saved.content.decode()
    duplicate = native_review(
        world, user, {**payload, **answers, "action": "rate"}, enhanced=enhanced
    )
    assert duplicate.status_code == 409
    with as_reviewer(world, user):
        assert EvaluationRating.objects.filter(run=world.run).count() == 1


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "hx"])
def test_http_missing_ratings_return_validation_400_with_valid_csrf(
    rbac_graph: RbacGraph, tmp_path: Path, enhanced: bool
) -> None:
    world = evaluation_world(rbac_graph)
    url = reverse("ai:evaluation", args=(world.clinic,))
    with signed_in(world.reviewers.rater_one) as client:
        client.handler.enforce_csrf_checks = True
        assert client.get(url).status_code == 200
        selection = {
            "action": "open",
            "run_id": str(world.run.pk),
            "case_id": "synthetic-accents",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        }
        opened = client.post(
            url, selection, HTTP_HX_REQUEST="true" if enhanced else "false"
        )
        assert opened.status_code == 200
        invalid = client.post(
            url,
            dict(selection, action="rate"),
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert invalid.status_code == 400
        form = invalid.context["form"]
        assert set(form.errors) == {*ERROR_LABELS, "correction_ms"}
        errors = form.errors.as_data()
        assert all(
            error.code == "required" for field in errors.values() for error in field
        )
        assert (
            gettext("Answer every review item before saving.")
            in invalid.content.decode()
        )
        assert set(invalid["Cache-Control"].split(", ")) == {
            "private",
            "no-store",
            "no-cache",
            "must-revalidate",
        }
        with tenant_context(world.reviewers.rater_one, world.organization):
            assert not EvaluationRating.objects.filter(run=world.run).exists()
        redacted = re.sub(
            r'\bvalue="[^"]*"', 'value="[redacted]"', invalid.content.decode()
        )
        redacted = re.sub(
            r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
            "[synthetic-id]",
            redacted,
        )
        assert selection["csrfmiddlewaretoken"] not in redacted
        assert str(world.run.pk) not in redacted
        accepted = client.post(
            url,
            {**selection, **no_errors(), "action": "rate"},
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert accepted.status_code == 200
        missing_csrf = client.post(
            url,
            {
                key: value
                for key, value in selection.items()
                if key != "csrfmiddlewaretoken"
            },
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
        assert missing_csrf.status_code == 403
    with signed_in(world.owner) as creator:
        denied = creator.post(
            url, selection, HTTP_HX_REQUEST="true" if enhanced else "false"
        )
        assert denied.status_code == 403
    destination = Path(
        os.environ.get("CLINIC_T47_OFFLINE_EVIDENCE_ROOT", str(tmp_path))
    )
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = "hx" if enhanced else "native"
    (destination / f"invalid-{mode}.redacted.html").write_text(
        redacted, encoding="utf-8"
    )
    (destination / f"invalid-{mode}.json").write_text(
        json.dumps(
            {
                "first_actor": "assigned_rater_one",
                "creator_is_distinct": world.owner != world.reviewers.rater_one,
                "open_status": opened.status_code,
                "missing_ratings_status": invalid.status_code,
                "field_errors": {
                    name: [error.code for error in field]
                    for name, field in errors.items()
                },
                "cache_control": invalid["Cache-Control"],
                "complete_rating_status": accepted.status_code,
                "missing_csrf_status": missing_csrf.status_code,
                "creator_open_status": denied.status_code,
                "secrets_redacted": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_native_refusals_cover_malformed_unknown_unassigned_and_extra_inputs(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    user = world.reviewers.rater_one
    for payload, expected in (
        ({"action": "open", "run_id": "not-a-uuid"}, 409),
        ({"action": "open", "run_id": str(uuid4())}, 403),
        ({"action": "unknown", "run_id": str(world.run.pk)}, 400),
        ({"action": "open", "run_id": str(world.run.pk), "extra": "x"}, 400),
        ({"action": "open", "run_id": str(world.run.pk), "case_id": "unknown"}, 409),
    ):
        response = native_review(world, user, payload)
        assert response.status_code == expected
        assert response["Cache-Control"] == "private, no-store"
    # An owner can decide changes, but cannot pretend to be an assigned rater.
    response = native_review(
        world, world.owner, {"action": "open", "run_id": str(world.run.pk)}
    )
    assert response.status_code == 403
    with as_reviewer(world, world.owner):
        change = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    refused = native_review(
        world,
        world.approver,
        {
            "action": "approved",
            "change_id": str(change.pk),
            "approval_ref": "synthetic-no-human-review",
        },
    )
    assert refused.status_code == 409
    rejected = native_review(
        world,
        world.approver,
        {
            "action": "rejected",
            "change_id": str(change.pk),
            "approval_ref": "synthetic-owner-rejection",
        },
    )
    assert rejected.status_code == 200
    assert "data-evaluation-saved" in rejected.content.decode()


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "hx"])
def test_http_unknown_review_actions_are_read_only_denials(
    rbac_graph: RbacGraph, enhanced: bool
) -> None:
    world = evaluation_world(rbac_graph)
    url = reverse("ai:evaluation", args=(world.clinic,))
    with signed_in(world.reviewers.rater_one) as client:
        assert client.get(url).status_code == 200
        snapshot = dict(client.session)
        before = committed_state()
        denials = []
        for action in ("unknown", "unregistered"):
            response = client.post(
                url,
                {
                    "action": action,
                    "run_id": str(world.run.pk),
                    "case_id": "synthetic-accents",
                },
                HTTP_HX_REQUEST="true" if enhanced else "false",
            )
            assert response.status_code == 400
            assert_session_read_only(client, response, snapshot)
            denials.append(_normalized_denial(response))
            assert committed_state() == before
        assert denials[0] == denials[1]


def test_adjudicator_only_receives_votes_after_both_independent_reviews(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    selection = {
        "action": "open",
        "run_id": str(world.run.pk),
        "case_id": "synthetic-accents",
    }
    for user in (world.reviewers.rater_one, world.reviewers.rater_two):
        labels = no_errors()
        labels["negation"] = int(user == world.reviewers.rater_two)
        with as_reviewer(world, user):
            rate_case(
                clinic_id=world.clinic,
                run_id=world.run.pk,
                case_id="synthetic-accents",
                labels=labels,
            )
        opened = native_review(world, world.reviewers.adjudicator, selection)
        assert opened.status_code == 200
        assert ("data-evaluation-rating" in opened.content.decode()) is (
            user == world.reviewers.rater_two
        )


def test_owner_queue_binds_only_independent_passed_run_metadata(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    with as_reviewer(world, world.owner):
        passed = request_change(clinic_id=world.clinic, run_id=world.run.pk)
    marker = f'data-evaluation-change="{passed.pk}"'
    assert marker not in native_review(world, world.approver, None).content.decode()
    assert complete_reviews(world).status == "passed"
    assert marker not in native_review(world, world.owner, None).content.decode()
    html = native_review(world, world.approver, None).content.decode()
    assert marker in html
    for key, value in (
        ("capability", world.protocol.capability),
        ("configuration-version", str(world.run.configuration_version)),
        ("status", "passed"),
        ("thresholds-hash", world.protocol.thresholds_sha256),
    ):
        assert f'data-evaluation-{key}="{value}"' in html
    failed = review_world(
        organization=world.organization,
        clinic=world.clinic,
        configuration=world.configuration,
    )
    for user in (failed.reviewers.rater_one, failed.reviewers.rater_two):
        with as_reviewer(failed, user):
            for case in load_corpus("engineering-v1").cases:
                labels = no_errors()
                labels["critical_error"] = 1
                rate_case(
                    clinic_id=failed.clinic,
                    run_id=failed.run.pk,
                    case_id=case["id"],
                    labels=labels,
                )
    with as_reviewer(failed, failed.owner):
        assert (
            finish_run(clinic_id=failed.clinic, run_id=failed.run.pk).status == "failed"
        )
        rejected = request_change(clinic_id=failed.clinic, run_id=failed.run.pk)
    html = native_review(world, world.approver, None).content.decode()
    assert marker in html
    assert f'data-evaluation-change="{rejected.pk}"' not in html

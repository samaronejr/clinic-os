"""Finance native-form surface over real sessions, CSRF, RLS and human approval."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import revenue_views
from apps.ai.models import AIInvocation, Approval
from apps.ai.revenue_cycle import collect_revenue_cycle, propose_revenue_action
from apps.core.integration import execute_operation
from apps.insurance.models import Appeal
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from ai.revenue_support import (
    add_clinic_manager_role,
    request_and_collect,
    withdraw_finance_automation,
)
from ai.test_revenue_cycle import finance_setup, proposal_for, world
from billing.journal_support import finance_scope
from core.test_navigation import _client_for
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.models import ActionProposal
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def actual_step_up_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # fixed_otp_time patches the shared time module for token reproducibility.
    # Approval freshness still uses the real wall clock checked by PostgreSQL.
    monkeypatch.setattr(
        "apps.identity.stepup._utc_now_seconds",
        lambda: int(timezone.now().timestamp()),
    )


def post(
    client: Client, url: str, values: dict[str, str]
) -> _MonkeyPatchedWSGIResponse:
    with runtime_role(), fixed_otp_time():
        return client.post(
            url,
            {
                "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
                **values,
            },
        )


def test_native_finance_review_requires_preview_and_exact_human_approval(
    world: RevenueWorld,
) -> None:
    setup = world.setup
    url = reverse("ai:revenue-cycle", args=(setup.clinic_id,))
    client = verified_client(setup.graph)
    with runtime_role(), fixed_otp_time():
        page = client.get(url)
    assert page.status_code == 200
    assert {"private", "no-store"} <= set(
        page["Cache-Control"].replace(" ", "").split(",")
    )
    assert gettext("Revenue-cycle assistant").encode() in page.content
    queued = post(
        client, url, {"command": "request", "guide_id": str(world.cycle.guide.pk)}
    )
    assert queued.status_code == 200
    with finance_scope(setup):
        invocation = AIInvocation.objects.get(pk=queued.context["review"].invocation_id)
        before = Appeal.objects.count()
    assert invocation.operation_id is not None
    with runtime_role():
        execute_operation(invocation.operation_id)
    ready = post(
        client, url, {"command": "refresh", "invocation_id": str(invocation.pk)}
    )
    assert ready.status_code == 200
    assert ready.context["review"].state == "ready"
    review = ready.context["review"]
    item = next(item for item in review.items if item.kind == "appeal.draft")
    preview = post(
        client,
        url,
        {
            "command": "propose",
            "output_id": str(review.output_id),
            "position": str(item.position),
        },
    )
    assert preview.status_code == 200
    proposal = preview.context["proposal"]
    with finance_scope(setup):
        assert Appeal.objects.count() == before
    refused = post(
        client,
        url,
        {
            "command": "approve",
            "proposal_id": str(proposal.proposal_id),
            "digest": "0" * 64,
        },
    )
    assert refused.status_code == 409
    approved = post(
        client,
        url,
        {
            "command": "approve",
            "proposal_id": str(proposal.proposal_id),
            "digest": proposal.digest,
        },
    )
    assert approved.status_code == 200
    assert approved.context["proposal"].state == "receipt"
    with finance_scope(setup):
        assert Appeal.objects.count() == before + 1
        appeal = Appeal.objects.order_by("-created_at").first()
        assert appeal is not None
        assert appeal.state == "draft"


def test_selectors_are_post_only_and_denials_are_uniform(world: RevenueWorld) -> None:
    url = reverse("ai:revenue-cycle", args=(world.setup.clinic_id,))
    client = verified_client(world.setup.graph)
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
        assert (
            client.get(url, {"guide_id": str(world.cycle.guide.pk)}).status_code == 400
        )
    assert (
        post(client, url, {"command": "request", "guide_id": str(uuid4())}).status_code
        == 403
    )
    physician, _user = _client_for(world.setup.graph, "physician")
    with runtime_role(), fixed_otp_time():
        assert physician.get(url).status_code == 403
    invalid = post(
        client,
        url,
        {
            "command": "request",
            "guide_id": str(world.cycle.guide.pk),
            "body": "untrusted",
        },
    )
    assert invalid.status_code == 400


def test_native_review_includes_translated_missing_evidence_warning(
    world: RevenueWorld,
) -> None:
    review = request_and_collect(world)
    assert review.invocation_id is not None
    with finance_scope(world.setup):
        current = collect_revenue_cycle(
            clinic_id=world.setup.clinic_id, invocation_id=review.invocation_id
        )
    assert current.state == "ready"
    client = verified_client(world.setup.graph)
    url = reverse("ai:revenue-cycle", args=(world.setup.clinic_id,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    response = post(
        client, url, {"command": "refresh", "invocation_id": str(review.invocation_id)}
    )
    assert (
        gettext(
            "Clinical supporting evidence is not retrieved. Missing evidence means "
            "not found in the supplied sources, never absence of care."
        ).encode()
        in response.content
    )


def test_revenue_page_refuses_decisions_without_automation_finance(
    world: RevenueWorld,
) -> None:
    setup = world.setup
    # A clinic manager who bills is automation staff, unlike a Finance-only user.
    add_clinic_manager_role(setup)
    review = request_and_collect(world)
    with finance_scope(setup):
        proposal = proposal_for(world, review)
    withdraw_finance_automation(setup)
    # A fresh session: the owner's subtraction revoked the earlier ones.
    client = verified_client(setup.graph)
    url = reverse("ai:revenue-cycle", args=(setup.clinic_id,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    for command in ("approve", "reject"):
        response = post(
            client,
            url,
            {
                "command": command,
                "proposal_id": str(proposal.pk),
                "digest": proposal.digest,
            },
        )
        assert response.status_code == 403
    with owner_context(setup.graph.organization_a):
        proposal.refresh_from_db()
        assert proposal.state == "validated"
        assert not Approval.objects.filter(proposal=proposal).exists()


def test_revenue_decisions_on_an_unknown_proposal_refuse_uniformly(
    world: RevenueWorld,
) -> None:
    client = verified_client(world.setup.graph)
    url = reverse("ai:revenue-cycle", args=(world.setup.clinic_id,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    for command in ("approve", "reject"):
        values = {"command": command, "proposal_id": str(uuid4()), "digest": "0" * 64}
        assert post(client, url, values).status_code == 403


@pytest.mark.parametrize("inconsistency", ["capability", "output"])
def test_revenue_preview_refuses_a_proposal_it_cannot_bind(
    world: RevenueWorld, monkeypatch: pytest.MonkeyPatch, inconsistency: str
) -> None:
    """A preview binds only a revenue proposal to this clinic's own output."""
    review = request_and_collect(world)
    item = next(item for item in review.items if item.kind == "appeal.draft")
    client = verified_client(world.setup.graph)
    url = reverse("ai:revenue-cycle", args=(world.setup.clinic_id,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    if inconsistency == "capability":
        original = propose_revenue_action

        def other_capability(
            *, clinic_id: UUID, output_id: UUID, position: int
        ) -> ActionProposal:
            # Only the view's copy differs; the stored policy stays revenue_cycle.
            proposal = original(
                clinic_id=clinic_id, output_id=output_id, position=position
            )
            proposal.run.policy.capability = "front_desk"
            return proposal

        monkeypatch.setattr(revenue_views, "propose_revenue_action", other_capability)
    else:
        # The previewed subject resolves to no output artifact of this clinic.
        monkeypatch.setattr(revenue_views, "identifier", lambda _value: uuid4())
    values = {
        "command": "propose",
        "output_id": str(review.output_id),
        "position": str(item.position),
    }
    assert post(client, url, values).status_code == 403

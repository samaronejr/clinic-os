"""AI-off finance staff can review the actual denial and draft without submitting."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.controls import set_organization_policy
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.owner import set_global_switch
from apps.ai.policy import OrganizationPolicy
from apps.billing.models import JournalEntry
from apps.identity.clinic_configuration import latest_configuration
from apps.insurance.models import Appeal
from django.urls import reverse

from ai.test_revenue_views import finance_setup, post, world
from billing.journal_support import finance_scope
from identity.lifecycle_test_support import verified_client
from identity.permission_support import permission_context
from otp_test_support import fixed_otp_time, runtime_role

if TYPE_CHECKING:
    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("switch", ["global", "organization"])
def test_manual_appeal_with_ai_off(world: RevenueWorld, switch: str) -> None:
    setup = world.setup
    if switch == "global":
        set_global_switch(enabled=False, approved_regions=())
    else:
        with permission_context(setup.graph, world.administrator):
            current = latest_configuration(setup.clinic_id)
            assert current is not None
            set_organization_policy(
                clinic_id=setup.clinic_id,
                expected_version=current.version,
                policy=OrganizationPolicy(),
            )
    url = reverse("ai:revenue-cycle", args=(setup.clinic_id,))
    client = verified_client(setup.graph)
    denial = world.cycle.glosa
    with finance_scope(setup):
        world.cycle.guide.refresh_from_db()
        denial.refresh_from_db()
        before = (
            AIInvocation.objects.count(),
            AIArtifact.objects.count(),
            JournalEntry.objects.count(),
            world.cycle.guide.state,
            denial.code,
        )
    with runtime_role(), fixed_otp_time():
        finance = client.get(reverse("billing:finance", args=(setup.clinic_id,)))
        page = client.get(url)
    assert finance.status_code == 200
    assert b"data-manual-claim-link" in finance.content
    assert page.status_code == 200
    assert b"data-revenue-unavailable" in page.content
    url = reverse("ai:manual-claims", args=(setup.clinic_id,))
    assert url.encode() in finance.content
    preview = post(
        client, url, {"command": "inspect", "guide_id": str(world.cycle.guide.pk)}
    )
    assert preview.status_code == 200
    assert str(denial.pk).encode() in preview.content
    body = "Synthetic administrative appeal of the recorded denial."
    command = {
        "command": "draft",
        "glosa_id": str(denial.pk),
        "body": body,
        "command_id": str(uuid4()),
    }
    for _ in range(2):
        saved = post(client, url, command)
        assert saved.status_code == 200
    assert (
        post(client, url, {**command, "body": "Different synthetic bytes"}).status_code
        == 409
    )
    assert (
        post(client, url, {**command, "clinical_assertions": "Not allowed"}).status_code
        == 400
    )
    assert post(client, url, {**command, "command": "submit"}).status_code == 400
    with finance_scope(setup):
        draft = Appeal.objects.get(command_id=command["command_id"])
        assert (draft.glosa_id, draft.state, draft.body, draft.clinical_assertions) == (
            denial.pk,
            "draft",
            body,
            "",
        )
        world.cycle.guide.refresh_from_db()
        denial.refresh_from_db()
        assert (
            AIInvocation.objects.count(),
            AIArtifact.objects.count(),
            JournalEntry.objects.count(),
            world.cycle.guide.state,
            denial.code,
        ) == before

"""Care coordination over real HTTP: POST-body selectors and native refusals."""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.ai.action_validation import database_time
from apps.ai.care_models import CareProposal
from apps.core import workspace
from apps.core.navigation import granted_permissions
from apps.identity import stepup
from apps.identity.models import User
from apps.workflows.care.models import CareAction
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext

from ai.care_support import CareWorld, care_world, run_model
from core.test_navigation import _get, _post, _reauthenticate
from core.test_workspace_boundaries import _normalized_denial, remove_permission
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time, runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from workflows.permission_support import permission_actor

if TYPE_CHECKING:
    from pathlib import Path

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
INVOCATION = re.compile(r'name="invocation_id" value="([0-9a-f-]{36})"')
DIGEST = re.compile(r'name="digest" value="([0-9a-f]{64})"')
CONFLICT = (
    "These suggestions changed or are no longer eligible. "
    "Review them again or continue manually."
)


@pytest.fixture
def world(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> CareWorld:
    # One fixed clock for the login verification and every later step-up
    # check, set to the database time that ages the approval evidence.
    now = int(database_time().timestamp())
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: now)
    return care_world(rbac_graph, monkeypatch, synthetic_secret_backend)


def _client(user_id: UUID, role: str) -> Client:
    # Permission actors are created without a credential; sign in like staff.
    User.objects.filter(pk=user_id).update(password=make_password(RBAC_RAW_CREDENTIAL))
    client = Client(enforce_csrf_checks=True, raise_request_exception=False)
    client.cookies[settings.CSRF_COOKIE_NAME] = secrets.token_hex(16)
    _reauthenticate(client, user_id, role)
    return client


def _url(world: CareWorld) -> str:
    return reverse("ai:care-coordination", args=(world.graph.clinic_a,))


def _items(world: CareWorld) -> list[CareProposal]:
    with owner_context(world.graph.organization_a):
        return list(CareProposal.objects.order_by("created_at", "position"))


def _requested(world: CareWorld, client: Client) -> str:
    response = _post(
        client, _url(world), {"action": "request", "plan_id": str(world.plan.pk)}
    )
    assert response.status_code == 200
    assert 'data-care-bundle="generating"' in response.content.decode()
    match = INVOCATION.search(response.content.decode())
    assert match is not None
    invocation = match.group(1)
    assert run_model(world, UUID(invocation)) == "succeeded"
    return invocation


def _body(response: _MonkeyPatchedWSGIResponse) -> str:
    cache = {part.strip() for part in response["Cache-Control"].split(",")}
    assert {"private", "no-store"} <= cache
    return response.content.decode()


def test_clinician_requests_and_staff_approve_the_bundle_over_http(
    world: CareWorld,
) -> None:
    url = _url(world)
    physician = _client(world.physician, "physician")
    page = _body(_get(physician, url))
    assert f'data-care-plan="{world.plan.pk}"' in page
    assert gettext("Request suggestions") in page
    invocation = _requested(world, physician)
    ready = _body(
        _post(physician, url, {"action": "refresh", "invocation_id": invocation})
    )
    assert 'data-care-bundle="ready"' in ready
    assert ready.count("data-care-item=") == 3
    manager = _client(world.manager, "clinic_manager")
    queue = _body(_get(manager, url))
    assert gettext("Review bundle") in queue
    # Staff open only the administrative items of the run.
    opened = _body(_post(manager, url, {"action": "open", "invocation_id": invocation}))
    assert opened.count("data-care-item=") == 2
    assert 'data-care-kind="questionnaire"' not in opened
    preview = _body(
        _post(manager, url, {"action": "preview", "invocation_id": invocation})
    )
    assert preview.count("data-care-preview-item=") == 2
    digest = DIGEST.search(preview)
    assert digest is not None
    approved = _body(
        _post(
            manager,
            url,
            {
                "action": "approve",
                "invocation_id": invocation,
                "digest": digest.group(1),
            },
        )
    )
    assert approved.count('data-care-outcome="approved"') == 2
    assert gettext("Approved by staff") in approved
    clinical = next(item for item in _items(world) if item.audience == "clinical")
    accepted = _body(
        _post(
            physician,
            url,
            {
                "action": "decide",
                "item_id": str(clinical.pk),
                "decision": "accept",
                "revision": str(clinical.revision),
            },
        )
    )
    assert "data-care-saved" in accepted
    assert {item.state for item in _items(world)} == {"approved", "accepted"}
    with owner_context(world.graph.organization_a):
        kinds = set(
            CareAction.objects.filter(plan_id=world.plan.pk).values_list(
                "kind", flat=True
            )
        )
    assert {"return_window", "staff_coordination", "questionnaire"} <= kinds


def test_stale_step_up_challenges_before_any_bundle_effect(
    world: CareWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _url(world)
    physician = _client(world.physician, "physician")
    invocation = _requested(world, physician)
    _post(physician, url, {"action": "refresh", "invocation_id": invocation})
    manager = _client(world.manager, "clinic_manager")
    preview = _body(
        _post(manager, url, {"action": "preview", "invocation_id": invocation})
    )
    digest = DIGEST.search(preview)
    assert digest is not None
    stale = stepup._utc_now_seconds() + stepup.DEFAULT_STEP_UP_MAX_AGE_SECONDS + 1
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: stale)
    response = _post(
        manager,
        url,
        {"action": "approve", "invocation_id": invocation, "digest": digest.group(1)},
    )
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("identity:step-up"))
    assert {item.state for item in _items(world) if item.audience != "clinical"} == {
        "pending"
    }


def test_native_refusals_roll_back_and_share_one_denial(world: CareWorld) -> None:
    url = _url(world)
    physician = _client(world.physician, "physician")
    invocation = _requested(world, physician)
    _post(physician, url, {"action": "refresh", "invocation_id": invocation})
    before = [(item.pk, item.state, item.revision) for item in _items(world)]
    clinical = next(item for item in _items(world) if item.audience == "clinical")
    decide = {
        "action": "decide",
        "item_id": str(clinical.pk),
        "decision": "accept",
        "revision": str(clinical.revision),
    }
    for payload in (
        {"action": "unknown"},
        {**decide, "extra": "synthetic"},
    ):
        assert _post(physician, url, payload).status_code == 400
    # A cross-site form without the CSRF token never reaches the view.
    with runtime_role(), fixed_otp_time():
        assert physician.post(url, decide).status_code == 403
    stale = _post(physician, url, {**decide, "revision": str(clinical.revision + 1)})
    assert stale.status_code == 409
    assert gettext(CONFLICT) in _body(stale)
    # Another clinician of the clinic sees neither this run nor an unknown one.
    other, _enrollment = permission_actor(world.graph, "physician")
    stranger = _client(other, "physician")
    denials = [
        _post(stranger, url, {"action": "open", "invocation_id": selector})
        for selector in (invocation, "00000000-0000-4000-8000-000000000000")
    ]
    assert [response.status_code for response in denials] == [403, 403]
    assert _normalized_denial(denials[0]) == _normalized_denial(denials[1])
    assert [(item.pk, item.state, item.revision) for item in _items(world)] == before
    # Without any care authority even a malformed command is a plain denial.
    remove_permission(world.graph, "physician", "automation.propose_clinical")
    revoked = _client(world.physician, "physician")
    assert _get(revoked, url).status_code == 403
    assert _post(revoked, url, {**decide, "decision": "maybe"}).status_code == 403
    assert [(item.pk, item.state, item.revision) for item in _items(world)] == before


def test_page_rechecks_authority_when_the_workspace_gate_admits(
    world: CareWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page decides again: an admitting gate never opens it to others."""
    receptionist, _enrollment = permission_actor(world.graph, "receptionist")
    client = _client(receptionist, "receptionist")
    monkeypatch.setattr(
        workspace,
        "granted_permissions",
        lambda clinic_id: (
            granted_permissions(clinic_id) | {"automation.propose_clinical"}
        ),
    )
    before = len(_items(world))
    assert _get(client, _url(world)).status_code == 403
    refused = _post(
        client, _url(world), {"action": "open", "invocation_id": "not-a-uuid"}
    )
    assert refused.status_code == 403
    assert len(_items(world)) == before

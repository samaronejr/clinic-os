"""Chart injection cannot change source authority, patient binding or the session."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from apps.ai.adapters import SyntheticProvider
from apps.ai.artifacts import read_artifact
from apps.ai.brief import display_brief
from apps.ai.context import assemble_context
from apps.ai.contracts import GatewayAccessDeniedError, ProviderResult, Purpose
from apps.ai.models import ActionExecution, ActionReceipt, AIArtifact, AIInvocation
from apps.core.api.errors import ui_api_denial_response
from apps.ehr.history import HistoryChange, save_history
from apps.ehr.note_access import grant_note_access, restrict_note, revoke_note_access
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.current_context import CurrentActorError
from apps.identity.models import CareTeamMembership
from apps.tenancy.db import tenant_context
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from ai.brief_support import BINDING, BriefWorld, display, finish, generate, seeded
from ai.security.corpus import CASES, VARIANTS, AttackCase, Variant
from ehr.longitudinal_support import care_reader, finalized
from ehr.test_addenda import clinician, signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_actor
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> BriefWorld:
    return seeded(rbac_graph, monkeypatch)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
@pytest.mark.parametrize("variant", VARIANTS)
def test_worst_case_chart_model_cannot_return_tools_or_exfiltration(
    world: BriefWorld,
    monkeypatch: pytest.MonkeyPatch,
    case: AttackCase,
    variant: Variant,
) -> None:
    g = world.graph
    source = case.source(variant)
    with as_actor(g, g.physician):
        save_history(
            clinic_id=g.clinic_a,
            encounter_id=world.chart.version.document.encounter_id,
            change=HistoryChange(
                "problem", 0, "documented", source.text, "active", "Synthetic"
            ),
        )
    original = SyntheticProvider.complete
    calls: list[str] = []

    def follows_source(
        self: SyntheticProvider, *, request: ProviderRequest
    ) -> ProviderResult:
        calls.append(case.identifier)
        valid = original(self, request=request)
        value = json.loads(valid.content)
        value["tool_calls"] = [case.model_patch]
        return ProviderResult(json.dumps(value), valid.usage)

    monkeypatch.setattr("apps.ai.adapters.SyntheticProvider.complete", follows_source)
    queued = generate(world)
    assert queued.invocation_id is not None
    finish(world, queued)
    assert calls == [case.identifier]
    assert display(world, queued).state == "unavailable"
    with as_actor(g, g.physician):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        assert (invocation.status, invocation.reason_code) == (
            "failed",
            "invalid_output",
        )
        assert not AIArtifact.objects.filter(kind="output").exists()
        assert not ActionExecution.objects.exists()
        assert not ActionReceipt.objects.exists()
        assert any(
            item.text == source.text
            for item in assemble_context(
                clinic_id=g.clinic_a, enrollment_id=world.chart.enrollment
            ).sources
        )


def test_foreign_artifact_is_as_unreadable_as_an_unknown_one(world: BriefWorld) -> None:
    g = world.graph
    sentinel = "SYNTHETIC-FOREIGN-CONTENT-49"
    with owner_context(g.organization_b), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [str(g.shared_user)],
        )
        foreign = AIArtifact.objects.create(
            organization_id=g.organization_b,
            clinic_id=g.clinic_c,
            user_id=g.shared_user,
            purpose=Purpose.ANALYST.value,
            content=sentinel,
            retention_class="transient_input",
            expires_at=timezone.now() + timedelta(days=1),
        )
        assert AIArtifact.objects.get(pk=foreign.pk).content == sentinel
    factory = RequestFactory()
    request = factory.post("/synthetic-artifact/", {"artifact_id": str(foreign.pk)})
    with runtime_role(), tenant_context(g.shared_user, g.organization_b):
        own = artifact_response(request, g.clinic_c)
        assert own.status_code == 200
        assert json.loads(own.content) == {"content": sentinel}
    errors = []
    responses = []
    with as_actor(g, g.physician):
        for selector in (foreign.pk, uuid4()):
            with pytest.raises(GatewayAccessDeniedError) as refused:
                read_artifact(clinic_id=g.clinic_a, artifact_id=selector)
            errors.append(str(refused.value))
            response = artifact_response(
                factory.post("/synthetic-artifact/", {"artifact_id": str(selector)}),
                g.clinic_a,
            )
            assert response.status_code == 403
            assert json.loads(response.content) == {
                "code": "access_denied",
                "message_key": "api.error.access_denied",
            }
            responses.append(
                (
                    str(response.status_code).encode(),
                    response.content,
                    json.dumps(dict(response.items()), sort_keys=True).encode(),
                )
            )
        assert not AIArtifact.objects.filter(pk=foreign.pk).exists()
    assert errors[0] == errors[1]
    assert responses[0] == responses[1]


def artifact_response(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Test-only transport: real artifact reads and the shared denial serializer.

    No production artifact HTTP route exists. Keep this bridge explicit rather
    than pretending an invocation selector is an artifact lookup.
    """
    try:
        content = read_artifact(
            clinic_id=clinic_id, artifact_id=UUID(request.POST["artifact_id"])
        )
    except GatewayAccessDeniedError:
        return ui_api_denial_response()
    return JsonResponse({"content": content})


def test_patient_and_session_switch_cannot_reuse_citations(world: BriefWorld) -> None:
    g = world.graph
    queued = generate(world)
    finish(world, queued)
    _other_user, other_enrollment = permission_actor(g, "physician")
    with owner_context(g.organization_a):
        CareTeamMembership.objects.create(
            organization_id=g.organization_a,
            clinic_id=g.clinic_a,
            patient_enrollment_id=other_enrollment,
            user_id=g.physician,
            role="physician",
            valid_from=timezone.now(),
        )
    assert queued.invocation_id is not None
    with as_actor(g, g.physician):
        # Both patients are individually authorized; the original turn is not portable.
        assert assemble_context(clinic_id=g.clinic_a, enrollment_id=other_enrollment)
        for enrollment, binding in (
            (other_enrollment, BINDING),
            (world.chart.enrollment, "2" * 64),
        ):
            with pytest.raises(GatewayAccessDeniedError):
                display_brief(
                    clinic_id=g.clinic_a,
                    enrollment_id=enrollment,
                    session_binding=binding,
                    invocation_id=queued.invocation_id,
                )
    assert display(world, queued).state == "ready"


def test_retrieval_never_borrows_foreign_clinic_authority(world: BriefWorld) -> None:
    g = world.graph
    errors = []
    with runtime_role(), tenant_context(g.shared_user, g.organization_b):
        for selector in (world.chart.enrollment, uuid4()):
            with pytest.raises(
                (CurrentActorError, ClinicalAccessDeniedError)
            ) as refused:
                assemble_context(clinic_id=g.clinic_a, enrollment_id=selector)
            errors.append(type(refused.value))
    assert errors[0] is errors[1]


def test_restricted_source_stays_hidden_after_an_injected_access_request(
    world: BriefWorld,
) -> None:
    g = world.graph
    note = finalized(g, world.chart.version)
    reader = care_reader(g, world.chart)
    with as_actor(g, g.physician):
        restrict_note(clinic_id=g.clinic_a, version_id=note.pk)
        grant = grant_note_access(
            clinic_id=g.clinic_a, version_id=note.pk, grantee_id=reader
        )
    queued = generate(world, user=reader)
    finish(world, queued)
    assert note.pk in {
        item.id for item in display(world, queued, user=reader).chart.sources
    }
    with as_actor(g, g.physician):
        revoke_note_access(clinic_id=g.clinic_a, grant_id=grant.pk)
    for case in CASES:
        refused = generate(world, user=reader, question=case.source("base64").text)
        assert refused.answer == "refused"
        assert refused.invocation_id is None
    ready = display(world, queued, user=reader)
    assert note.pk not in {item.id for item in ready.chart.sources}
    assert all("restricted" not in value for _key, value in ready.chart.coverage)


@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_unauthorized_chart_requests_leave_no_session_or_artifact_effect(
    world: BriefWorld, enhanced: bool
) -> None:
    g = world.graph
    receptionist = clinician(g, "receptionist", world.chart.enrollment)
    with owner_context(g.organization_a):
        before = (AIArtifact.objects.count(), AIInvocation.objects.count())
    url = reverse("ai:chart-assistant", args=(g.clinic_a,))
    with signed_in(receptionist) as client:
        session = dict(client.session)
        responses = []
        for selector in (world.chart.enrollment, uuid4()):
            response = client.post(
                url,
                {
                    "enrollment_id": str(selector),
                    "command": "generate",
                    "surface": "workspace",
                },
                HTTP_HX_REQUEST="true" if enhanced else "false",
            )
            assert response.status_code == 403
            assert b"data-chart-citation" not in response.content
            assert str(selector).encode() not in response.content
            assert dict(client.session) == session
            responses.append(response.content)
        assert responses[0] == responses[1]
    with owner_context(g.organization_a):
        assert (AIArtifact.objects.count(), AIInvocation.objects.count()) == before
    assert world.gateway.publications == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_injected_question_never_starts_an_unscoped_search(
    world: BriefWorld, case: AttackCase
) -> None:
    with (
        patch(
            "apps.ai.brief.assemble_context",
            side_effect=AssertionError("retrieval attempted"),
        ) as retrieve,
        patch(
            "apps.ai.brief.create_input",
            side_effect=AssertionError("artifact attempted"),
        ) as store,
    ):
        result = generate(world, question=case.source("plain").text)
    retrieve.assert_not_called()
    store.assert_not_called()
    assert (result.state, result.answer, result.invocation_id) == (
        "ready",
        "refused",
        None,
    )

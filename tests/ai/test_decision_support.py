"""Todo 46: no external gates are satisfied by these synthetic real-role proofs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.adapters import SyntheticProvider
from apps.ai.artifacts import create_input, read_artifact
from apps.ai.contracts import (
    GatewayAccessDeniedError,
    GatewayRefusedError,
    ProviderResult,
    Purpose,
)
from apps.ai.controls import set_organization_policy
from apps.ai.decision_contracts import DecisionRequest, parse_decision_request
from apps.ai.decision_policy import GATE_CAPABILITIES, authorized_catalog
from apps.ai.decision_support import (
    display_decision_support,
    open_decision_support,
    record_decision,
    request_decision_support,
)
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.owner import set_global_switch
from apps.ai.policy import organization_policy
from apps.ai.tasks import run_operation
from apps.consent.models import AIUseDisclosure
from apps.consent.services import record_ai_disclosure
from apps.ehr.note_access import grant_note_access, restrict_note, revoke_note_access
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import User, UserClinicRole
from apps.providers.lifecycle import ApprovalInput, approve_version
from apps.providers.models import CapabilityVersion, ProviderCapability
from apps.tenancy.db import tenant_context
from django.db import connection
from django.urls import reverse
from django.utils.translation import gettext

from ai.brief_support import BINDING, BriefWorld
from ai.decision_support_helpers import (
    counts,
    display,
    enable,
    finish,
    generate,
    revoke_gate,
    world_for,
)
from ai.serialization_support import SERIALIZATIONS, serialize
from ehr.longitudinal_support import care_reader, finalized, second_draft
from ehr.test_addenda import clinician, signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch) -> BriefWorld:
    return world_for(rbac_graph, monkeypatch)


@pytest.fixture
def ready(world: BriefWorld) -> BriefWorld:
    enable(world)
    return world


def test_catalog_requires_current_clinician_before_revealing_gate_state(
    ready: BriefWorld,
) -> None:
    g = ready.graph
    actor = User.objects.create(username="synthetic-catalog-no-membership")
    with owner_context(g.organization_a):
        assert not UserClinicRole.objects.filter(user=actor).exists()
    with as_actor(g, g.physician):
        assert authorized_catalog(clinic_id=g.clinic_a)
    with permission_context(g, actor.pk):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, current_setting('app.current_user_id')"
            )
            assert cursor.fetchone() == ("clinic_app", str(actor.pk))
        assert current_actor_id() == actor.pk
        for clinic in (g.clinic_a, g.clinic_b, g.clinic_c, uuid4()):
            with pytest.raises(CurrentActorError, match="current actor unauthorized"):
                authorized_catalog(clinic_id=clinic)


def test_optional_panel_defaults_off_and_each_clinician_request_requires_opt_in(
    world: BriefWorld,
) -> None:
    g = world.graph
    encounter = world.chart.version.document.encounter_id
    before = counts(world)
    assert generate(world, opt_in=False).state == "unavailable"
    assert generate(world).state == "unavailable"
    with as_actor(g, g.physician):
        assert (
            open_decision_support(
                clinic_id=g.clinic_a, encounter_id=encounter
            ).can_request
            is False
        )
    assert counts(world) == before
    enable(world)
    before = counts(world)
    with as_actor(g, g.physician):
        assert (
            open_decision_support(
                clinic_id=g.clinic_a, encounter_id=encounter
            ).can_request
            is True
        )
        assert (
            request_decision_support(
                clinic_id=g.clinic_a, encounter_id=encounter, session_binding=BINDING
            ).invocation_id
            is None
        )
    assert generate(world, opt_in=False).state == "unavailable"
    assert counts(world) == before
    assert world.gateway.publications == []


def test_fake_model_has_separate_facts_references_hypotheses_and_exact_licensed_sources(
    ready: BriefWorld,
) -> None:
    queued = generate(ready)
    assert queued.invocation_id is not None
    assert queued.state == "unavailable"
    assert display(ready, queued).state == "unavailable"
    assert finish(ready, queued) == "succeeded"
    panel = display(ready, queued)
    assert panel.state == "ready"
    assert panel.considerations
    for item in panel.considerations:
        assert item.kind == "review_concordance"
        assert item.fact.id
        assert item.fact.version
        assert item.fact.date
        assert item.reference.published_on == "2026-10-28"
        assert item.reference.population
        assert item.reference.limitations
        assert item.reference.license_ref == "CC0-1.0:clinic-os-synthetic-v1"
    with as_actor(ready.graph, ready.graph.physician):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        output = json.loads(
            read_artifact(
                clinic_id=ready.graph.clinic_a, artifact_id=invocation.output.pk
            )
        )
        assert set(output) == {
            "schema",
            "patient_facts",
            "reference_statements",
            "model_hypotheses",
        }
        assert invocation.output.expires_at == invocation.inputs_ref.expires_at
        assert (
            invocation.output.expires_at - invocation.inputs_ref.created_at
            <= timedelta(days=1)
        )
        assert SyntheticProvider.real_enabled is False


@pytest.mark.parametrize("missing", [key for key, _record in GATE_CAPABILITIES])
def test_capability_refuses_each_missing_authorization_without_side_effects(
    ready: BriefWorld,
    missing: str,
) -> None:
    with owner_context(ready.graph.organization_a):
        ProviderCapability.objects.filter(
            key=missing, clinic_id=ready.graph.clinic_a
        ).update(current_version=None)
    before = counts(ready)
    assert generate(ready).state == "unavailable"
    assert counts(ready) == before
    assert ready.gateway.publications == []


@pytest.mark.parametrize(
    "state", ["researched", "selected_in_plan", "approved_to_test", "sandbox"]
)
def test_non_authorized_lifecycle_states_never_enable_the_panel(
    ready: BriefWorld,
    state: str,
) -> None:
    key, record = GATE_CAPABILITIES[0]
    with owner_context(ready.graph.organization_a):
        capability = ProviderCapability.objects.get(
            key=key, clinic_id=ready.graph.clinic_a
        )
        current = capability.current_version
        assert current is not None
        candidate = CapabilityVersion.objects.create(
            capability=capability,
            provider="synthetic",
            account=uuid4().hex,
            environment="synthetic",
            state="researched",
        )
        if state != "researched":
            candidate.state = "selected_in_plan"
            candidate.save(update_fields=("state", "updated_at"))
        capability.current_version = candidate
        capability.save(update_fields=("current_version", "updated_at"))
    if state in {"approved_to_test", "sandbox"}:
        # The owner lifecycle helper, not a runtime UPDATE, binds the evidence.
        candidate = approve_version(
            key,
            clinic_id=ready.graph.clinic_a,
            decision=ApprovalInput(
                "Synthetic", "Synthetic owner", f"https://synthetic.invalid/{record}"
            ),
        )
        if state == "sandbox":
            candidate.state = state
            candidate.save(update_fields=("state", "updated_at"))
    before = counts(ready)
    assert generate(ready).state == "unavailable"
    assert counts(ready) == before


def test_reference_catalog_unavailable_is_an_honest_side_effect_free_refusal(
    ready: BriefWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise GatewayRefusedError(reason="configuration_unavailable")

    monkeypatch.setattr("apps.ai.decision_policy.load_catalog", unavailable)
    before = counts(ready)
    assert generate(ready).state == "unavailable"
    assert counts(ready) == before
    assert ready.gateway.publications == []


def test_fabricated_reference_response_is_dropped_before_storage_and_counted(
    ready: BriefWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SyntheticProvider.complete

    def fabricated(
        self: SyntheticProvider, *, request: ProviderRequest
    ) -> ProviderResult:
        result = original(self, request=request)
        data = json.loads(result.content)
        data["model_hypotheses"][0]["reference"]["key"] = "fabricated-citation"
        return ProviderResult(json.dumps(data), result.usage)

    monkeypatch.setattr("apps.ai.adapters.SyntheticProvider.complete", fabricated)
    queued = generate(ready)
    assert finish(ready, queued) == "failed"
    assert display(ready, queued).state == "unavailable"
    with owner_context(ready.graph.organization_a):
        assert (
            AIInvocation.objects.filter(
                purpose="decision_support",
                status="failed",
                reason_code="invalid_output",
            ).count()
            == 1
        )
        assert not AIArtifact.objects.filter(kind="output").exists()


@pytest.mark.parametrize("form", SERIALIZATIONS)
@pytest.mark.parametrize("attack", ["valid", "diagnosis", "citation"])
def test_serialized_gateway_response_is_validated_before_storage_and_counted(
    ready: BriefWorld,
    monkeypatch: pytest.MonkeyPatch,
    form: str,
    attack: str,
) -> None:
    original_encode = DecisionRequest.encode
    original_complete = SyntheticProvider.complete

    def encoded(self: DecisionRequest) -> str:
        return serialize(original_encode(self), form)

    def response(
        self: SyntheticProvider, *, request: ProviderRequest
    ) -> ProviderResult:
        result = original_complete(self, request=request)
        data = json.loads(result.content)
        if attack == "diagnosis":
            data["model_hypotheses"][0]["diagnosis"] = "Synthetic forbidden diagnosis"
        elif attack == "citation":
            data["model_hypotheses"][0]["reference"]["key"] = "fabricated-citation"
        return ProviderResult(serialize(json.dumps(data), form), result.usage)

    monkeypatch.setattr(DecisionRequest, "encode", encoded)
    monkeypatch.setattr(SyntheticProvider, "complete", response)
    queued = generate(ready)
    assert queued.invocation_id is not None
    expected = "succeeded" if attack == "valid" else "failed"
    assert finish(ready, queued) == expected
    assert display(ready, queued).state == (
        "ready" if attack == "valid" else "unavailable"
    )
    with owner_context(ready.graph.organization_a):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        assert invocation.status == expected
        if attack == "valid":
            assert AIArtifact.objects.filter(kind="output").count() == 1
        else:
            assert invocation.reason_code == "invalid_output"
            assert (
                AIInvocation.objects.filter(
                    purpose="decision_support",
                    status="failed",
                    reason_code="invalid_output",
                ).count()
                == 1
            )
            assert not AIArtifact.objects.filter(kind="output").exists()


def test_budget_refusal_rolls_back_input_chart_audit_and_publication(
    ready: BriefWorld,
) -> None:
    g = ready.graph
    with as_actor(g, ready.gateway.administrator):
        current = latest_configuration(g.clinic_a)
        assert current is not None
        policy = organization_policy()
        set_organization_policy(
            clinic_id=g.clinic_a,
            expected_version=current.version,
            policy=replace(
                policy,
                monthly_budget_micros=1,
                purpose_budgets=(("brief", 1), ("decision_support", 1)),
            ),
        )
    before = counts(ready)
    assert generate(ready).state == "unavailable"
    assert counts(ready) == before
    assert ready.gateway.publications == []


@pytest.mark.parametrize("stop", ["global", "gate"])
def test_stop_after_enqueue_prevents_dispatch_and_hides_the_result(
    ready: BriefWorld,
    stop: str,
) -> None:
    queued = generate(ready)
    if stop == "global":
        set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    else:
        revoke_gate(ready)
    assert finish(ready, queued) in {"cancelled", "failed", "skipped"}
    assert display(ready, queued).state == "unavailable"
    with owner_context(ready.graph.organization_a):
        assert not AIArtifact.objects.filter(kind="output").exists()


def test_direct_gateway_artifacts_cannot_bypass_gate_revocation(
    ready: BriefWorld,
) -> None:
    queued = generate(ready)
    assert queued.invocation_id is not None
    with as_actor(ready.graph, ready.graph.physician):
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        raw = invocation.inputs_ref.content
    revoke_gate(ready)
    before = counts(ready)
    with (
        as_actor(ready.graph, ready.graph.physician),
        pytest.raises(GatewayRefusedError),
    ):
        create_input(
            clinic_id=ready.graph.clinic_a,
            purpose=Purpose.DECISION_SUPPORT,
            patient_enrollment_id=ready.chart.enrollment,
            content=raw,
        )
    assert counts(ready) == before


@pytest.mark.parametrize("decision", ["accepted", "modified", "rejected"])
def test_clinician_choices_record_ai_use_without_changing_the_chart_or_session(
    ready: BriefWorld,
    decision: str,
) -> None:
    queued = generate(ready)
    assert finish(ready, queued) == "succeeded"
    assert queued.invocation_id is not None
    g = ready.graph
    with as_actor(g, g.physician):
        result = record_decision(
            clinic_id=g.clinic_a,
            encounter_id=ready.chart.version.document.encounter_id,
            session_binding=BINDING,
            invocation_id=queued.invocation_id,
            decision=decision,
        )
    assert result.decision == decision
    with owner_context(g.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE event_type=%s",
            [f"ai.decision_support.{decision}"],
        )
        assert cursor.fetchone() == (1,)
        ready.chart.version.refresh_from_db()
        assert ready.chart.version.revision == 1
        assert ready.chart.version.state == "draft"


@pytest.mark.parametrize("enhanced", [False, True])
def test_native_and_htmx_panel_require_session_bound_opt_in(
    ready: BriefWorld,
    enhanced: bool,
) -> None:
    g = ready.graph
    url = reverse("ai:decision-support", args=(g.clinic_a,))
    body = {
        "encounter_id": str(ready.chart.version.document.encounter_id),
        "command": "generate",
    }
    with signed_in(g.physician) as client:
        before_session = dict(client.session)
        enhancement = "true" if enhanced else "false"
        refused = client.post(url, body, HTTP_HX_REQUEST=enhancement)
        assert refused.status_code == 200
        assert dict(client.session) == before_session
        assert b"data-cds-pending" not in refused.content
        queued = client.post(
            url, {**body, "opt_in": "yes"}, HTTP_HX_REQUEST=enhancement
        )
        assert queued.status_code == 200
        assert b"data-cds-pending" in queued.content
        assert (b"<html" not in queued.content) == enhanced
        assert "no-store" in queued["Cache-Control"]
        with tenant_context(g.physician, g.organization_a):
            invocation = AIInvocation.objects.get(
                user_id=g.physician, purpose="decision_support"
            )
            operation_id = invocation.operation_id
        assert operation_id is not None
        assert run_operation(operation_id=str(operation_id)) == "succeeded"
        shown = client.post(
            url,
            {**body, "command": "display", "invocation_id": str(invocation.pk)},
            HTTP_HX_REQUEST=enhancement,
        )
        assert shown.status_code == 200
        assert b'data-cds-state="ready"' in shown.content
        assert gettext("Patient fact").encode() in shown.content
        assert b'data-cds-reference="synthetic-evidence-review"' in shown.content
        assert dict(client.session) == before_session


def test_http_refusals_preserve_session_and_have_no_counts_or_selectors(
    world: BriefWorld,
    superuser_database_url: str,
) -> None:
    g = world.graph
    receptionist = clinician(g, "receptionist", world.chart.enrollment)
    url = reverse("ai:decision-support", args=(g.clinic_a,))
    with signed_in(receptionist) as client:
        session = dict(client.session)
        before = counts(world, database_url=superuser_database_url)
        bodies = []
        for encounter in (world.chart.version.document.encounter_id, uuid4()):
            response = client.post(
                url, {"encounter_id": str(encounter), "command": "open"}
            )
            assert response.status_code == 403
            assert str(encounter).encode() not in response.content
            assert b"data-cds-fact" not in response.content
            bodies.append(response.content)
        assert bodies[0] == bodies[1]
        assert dict(client.session) == session
        assert counts(world, database_url=superuser_database_url) == before
        assert client.get(url).status_code == 405
        assert (
            client.post(
                url,
                {
                    "encounter_id": str(uuid4()),
                    "command": "open",
                    "authority": "physician",
                },
            ).status_code
            == 400
        )


def test_patient_refusal_never_becomes_a_clinician_opt_in(world: BriefWorld) -> None:
    enable(world)
    g = world.graph
    # Use a fresh encounter: disclosure rows are immutable, including refusals.
    draft = second_draft(g, world.chart, g.physician)
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=draft.document.encounter_id,
            informed=True,
            refused=True,
        )
        before = AIUseDisclosure.objects.count()
        panel = request_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=draft.document.encounter_id,
            session_binding=BINDING,
            opt_in=True,
        )
        assert AIUseDisclosure.objects.count() == before
    assert panel.state == "unavailable"
    assert panel.invocation_id is None
    assert world.gateway.publications == []


def test_session_and_other_patient_cannot_replay_a_clinician_result(
    ready: BriefWorld,
) -> None:
    queued = generate(ready)
    assert finish(ready, queued) == "succeeded"
    assert queued.invocation_id is not None
    g = ready.graph
    before = counts(ready)
    with as_actor(g, g.physician), pytest.raises(GatewayAccessDeniedError):
        display_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=ready.chart.version.document.encounter_id,
            session_binding="2" * 64,
            invocation_id=queued.invocation_id,
        )
    with (
        as_actor(g, g.shared_user),
        pytest.raises(
            (CurrentActorError, ClinicalAccessDeniedError, GatewayAccessDeniedError)
        ),
    ):
        display_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=ready.chart.version.document.encounter_id,
            session_binding=BINDING,
            invocation_id=queued.invocation_id,
        )
    assert counts(ready) == before


def test_restricted_fact_is_hidden_after_display_grant_is_revoked(
    ready: BriefWorld,
) -> None:
    g = ready.graph
    note = finalized(g, ready.chart.version)
    reader = care_reader(g, ready.chart)
    with as_actor(g, g.physician):
        restrict_note(clinic_id=g.clinic_a, version_id=note.pk)
        grant = grant_note_access(
            clinic_id=g.clinic_a, version_id=note.pk, grantee_id=reader
        )
    with as_actor(g, reader):
        queued = request_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=ready.chart.version.document.encounter_id,
            session_binding=BINDING,
            opt_in=True,
        )
    assert finish(ready, queued) == "succeeded"
    assert queued.invocation_id is not None
    with as_actor(g, g.physician):
        revoke_note_access(clinic_id=g.clinic_a, grant_id=grant.pk)
    with as_actor(g, reader):
        panel = display_decision_support(
            clinic_id=g.clinic_a,
            encounter_id=ready.chart.version.document.encounter_id,
            session_binding=BINDING,
            invocation_id=queued.invocation_id,
        )
        assert all(item.fact.id != note.pk for item in panel.considerations)
        invocation = AIInvocation.objects.get(pk=queued.invocation_id)
        assert (
            parse_decision_request(invocation.inputs_ref.content).chart.enrollment
            == ready.chart.enrollment
        )

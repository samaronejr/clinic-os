"""The physician's chart authority cannot substitute for either patient choice."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import psycopg
import pytest
from apps.ai import brief_views
from apps.ai.artifacts import read_artifact
from apps.ai.brief import display_brief, request_brief
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError, Purpose
from apps.ai.decision_support import (
    display_decision_support,
    record_decision,
    request_decision_support,
)
from apps.ai.models import AIArtifact, AIInvocation
from apps.ai.tasks import run_operation
from apps.audit.canonical import AUDIT_PAYLOAD_ALLOWED
from apps.consent.services import record_ai_disclosure, revoke_consent
from apps.ehr.finalization import close_encounter
from apps.ehr.services import record_clinical_note
from apps.intake.patient_access import patient_session_context
from apps.workflows.today import TodayAppointment, view_today
from django.urls import reverse
from django.utils import timezone

from ai.brief_support import BINDING, seeded, without_patient_choice
from ai.chart_consent_support import ConsentActors, accept_ai, choose_ai
from ai.decision_support_helpers import counts, enable, generate, world_for
from ehr.longitudinal_support import finalized, second_draft
from ehr.test_addenda import signed_in
from ehr.test_autosave import SECTIONS, as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("disclosure", [None, (False, False), (True, True)])
@pytest.mark.parametrize("enrollment_only", [False, True])
def test_brief_requires_this_encounters_disclosure(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    disclosure: tuple[bool, bool] | None,
    enrollment_only: bool,
    superuser_database_url: str,
) -> None:
    g = rbac_graph
    world = without_patient_choice(g, monkeypatch)
    accept_ai(g, world.chart.enrollment)
    encounter = world.chart.version.document.encounter_id
    if disclosure is not None:
        with as_actor(g, g.physician):
            record_ai_disclosure(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                informed=disclosure[0],
                refused=disclosure[1],
            )
    before = counts(world)[:3]
    with as_actor(g, g.physician):
        brief = request_brief(
            clinic_id=g.clinic_a,
            enrollment_id=world.chart.enrollment if enrollment_only else None,
            encounter_id=None if enrollment_only else encounter,
            session_binding=BINDING,
        )
    assert (brief.state, brief.invocation_id, brief.chart.sources) == (
        "unavailable",
        None,
        (),
    )
    assert counts(world)[:3] == before
    assert world.gateway.publications == []
    _denial_audit(superuser_database_url, g.clinic_a)
    with as_actor(g, g.physician):
        version = world.chart.version
        saved = record_clinical_note(
            clinic_id=g.clinic_a,
            version_id=version.pk,
            expected_revision=version.revision,
            content=SECTIONS,
        )
        assert saved.revision == version.revision + 1


def test_decision_opt_in_cannot_override_patient_revocation(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_for(rbac_graph, monkeypatch)
    enable(world)
    patient, receipt = accept_ai(rbac_graph, world.chart.enrollment)
    with runtime_role(), patient_session_context(patient):
        revoke_consent(acceptance_id=receipt)
    before = counts(world)[:3]
    panel = generate(world)
    assert not panel.can_request
    assert panel.invocation_id is None
    assert not panel.considerations
    assert counts(world)[:3] == before
    assert world.gateway.publications == []


def _denial_audit(database_url: str, clinic: UUID) -> None:
    with psycopg.connect(database_url) as inspect:
        rows = inspect.execute(
            "SELECT payload FROM clinic_app.audit_event "
            "WHERE event_type='ehr.access.denied' "
            "AND payload->>'reason_code'='ai_permission_unavailable'"
        ).fetchall()
    assert rows
    for (payload,) in rows:
        assert payload == {
            "clinic_id": str(clinic),
            "reason_code": "ai_permission_unavailable",
        }
        assert set(payload) <= AUDIT_PAYLOAD_ALLOWED


@pytest.mark.parametrize("purpose", [Purpose.BRIEF, Purpose.DECISION_SUPPORT])
@pytest.mark.parametrize("boundary", ["queued", "retained", "reconsented"])
def test_revocation_fences_queued_work_and_retained_output(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    purpose: Purpose,
    boundary: Literal["queued", "retained", "reconsented"],
) -> None:
    g = rbac_graph
    world = world_for(g, monkeypatch)
    enable(world)
    patient, receipt = accept_ai(g, world.chart.enrollment)
    encounter = world.chart.version.document.encounter_id
    with as_actor(g, g.physician):
        if purpose is Purpose.BRIEF:
            invocation_id = request_brief(
                clinic_id=g.clinic_a, encounter_id=encounter, session_binding=BINDING
            ).invocation_id
        else:
            invocation_id = request_decision_support(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                session_binding=BINDING,
                opt_in=True,
            ).invocation_id
        assert invocation_id is not None
        operation = AIInvocation.objects.get(pk=invocation_id).operation_id
    assert operation is not None
    output_ref = None
    if boundary != "queued":
        with runtime_role():
            assert run_operation(operation_id=str(operation)) == "succeeded"
        with as_actor(g, g.physician):
            output_ref = AIInvocation.objects.get(pk=invocation_id).output.pk
    with runtime_role(), patient_session_context(patient):
        revoke_consent(acceptance_id=receipt)
    if boundary == "reconsented":
        # New text and action must not revive outputs made under the old receipt.
        assert accept_ai(g, world.chart.enrollment)[1] != receipt
    if boundary == "queued":

        def no_call(*_args: object, **_kwargs: object) -> None:
            pytest.fail("revoked chart input reached the provider")

        monkeypatch.setattr("apps.ai.outbox.call_provider", no_call)
        with runtime_role():
            assert run_operation(operation_id=str(operation)) in {
                "cancelled",
                "skipped",
                "failed",
            }
        with owner_context(g.organization_a):
            invocation = AIInvocation.objects.get(pk=invocation_id)
            assert (
                invocation.status,
                invocation.cost_micros,
                invocation.reason_code,
            ) == ("failed", 0, "authority_revoked")
            assert not AIArtifact.objects.filter(invocation=invocation).exists()
    with as_actor(g, g.physician):
        invocation = AIInvocation.objects.get(pk=invocation_id)
        refs = [invocation.inputs_ref_id]
        if output_ref is not None:
            # Retain the pre-withdrawal handle. The ordinary relation now
            # withholds even the pending row's identifier on a fresh read.
            assert not AIArtifact.objects.filter(pk=output_ref).exists()
            refs.append(output_ref)
        for ref in refs:
            with pytest.raises(GatewayRefusedError):
                read_artifact(clinic_id=g.clinic_a, artifact_id=ref)
        if purpose is Purpose.BRIEF:
            brief = display_brief(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                invocation_id=invocation_id,
                session_binding=BINDING,
            )
            assert (brief.state, brief.chart.sources) == ("unavailable", ())
        else:
            panel = display_decision_support(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                invocation_id=invocation_id,
                session_binding=BINDING,
            )
            assert (panel.state, panel.considerations) == ("unavailable", ())
            with pytest.raises(GatewayRefusedError):
                record_decision(
                    clinic_id=g.clinic_a,
                    encounter_id=encounter,
                    invocation_id=invocation_id,
                    session_binding=BINDING,
                    decision="accepted",
                )


def test_no_purpose_acceptance_or_separate_refusal_blocks_both_chart_consumers(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    world = world_for(g, monkeypatch)
    enable(world)
    choose_ai(
        ConsentActors(g.organization_a, g.clinic_a, g.clinic_admin, g.shared_user),
        world.chart.enrollment,
        accept=False,
    )
    assert generate(world).invocation_id is None
    with as_actor(g, g.physician):
        assert (
            request_brief(
                clinic_id=g.clinic_a,
                encounter_id=world.chart.version.document.encounter_id,
                session_binding=BINDING,
            ).state
            == "unavailable"
        )
    assert not world.gateway.publications


@pytest.mark.parametrize("purpose", [Purpose.BRIEF, Purpose.DECISION_SUPPORT])
@pytest.mark.parametrize("wrong", ["encounter", "session"])
def test_wrong_display_scope_denied_even_after_the_original_receipt_is_revoked(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, purpose: Purpose, wrong: str
) -> None:
    g = rbac_graph
    world = world_for(g, monkeypatch)
    enable(world)
    owner, receipt = accept_ai(g, world.chart.enrollment)
    encounter = world.chart.version.document.encounter_id
    with as_actor(g, g.physician):
        if purpose is Purpose.BRIEF:
            key = request_brief(
                clinic_id=g.clinic_a, encounter_id=encounter, session_binding=BINDING
            ).invocation_id
        else:
            key = request_decision_support(
                clinic_id=g.clinic_a,
                encounter_id=encounter,
                session_binding=BINDING,
                opt_in=True,
            ).invocation_id
    assert key is not None
    if wrong == "encounter":
        encounter = second_draft(g, world.chart, g.physician).document.encounter_id
    binding = "2" * 64 if wrong == "session" else BINDING
    with runtime_role(), patient_session_context(owner):
        revoke_consent(acceptance_id=receipt)
    # An out-of-scope caller must not even learn whether the old receipt
    # remains current. For the right visit/session, withdrawal is "unavailable".
    display = display_brief if purpose is Purpose.BRIEF else display_decision_support
    with as_actor(g, g.physician), pytest.raises(GatewayAccessDeniedError):
        display(
            clinic_id=g.clinic_a,
            encounter_id=encounter,
            invocation_id=key,
            session_binding=binding,
        )


def test_today_cannot_borrow_an_open_visit_from_a_different_appointment(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    world = seeded(g, monkeypatch)
    with as_actor(g, g.physician):
        actual = view_today(clinic_id=g.clinic_a)
    # Isolate only the agenda selection: this patient has an eligible open
    # visit, but the selected upcoming appointment has no encounter at all.
    appointment = TodayAppointment(
        uuid4(),
        timezone.now(),
        "12:00",
        "12:30",
        "Synthetic",
        "scheduled",
        encounter_open=False,
        can_start=True,
        enrollment_id=world.chart.enrollment,
    )
    monkeypatch.setattr(
        brief_views, "view_today", lambda **_: replace(actual, next_patient=appointment)
    )
    with signed_in(g.physician) as client:
        response = client.post(
            reverse("ai:chart-assistant", args=(g.clinic_a,)),
            {
                "command": "generate",
                "surface": "today",
                "enrollment_id": str(world.chart.enrollment),
            },
        )
        assert response.status_code == 200
        assert b'data-today-brief="unavailable"' in response.content
    assert world.gateway.publications == []


def test_workspace_visit_switch_denies_before_either_visits_consent(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    world = seeded(g, monkeypatch)
    encounter = world.chart.version.document.encounter_id
    with as_actor(g, g.physician):
        key = request_brief(
            clinic_id=g.clinic_a, encounter_id=encounter, session_binding=BINDING
        ).invocation_id
    assert key is not None
    finalized(g, world.chart.version)
    with as_actor(g, g.physician):
        close_encounter(clinic_id=g.clinic_a, encounter_id=encounter)
    # The only open visit belongs to the same patient but has no disclosure.
    second_draft(g, world.chart, g.physician)
    with as_actor(g, g.physician), pytest.raises(GatewayAccessDeniedError):
        display_brief(
            clinic_id=g.clinic_a,
            enrollment_id=world.chart.enrollment,
            invocation_id=key,
            session_binding=BINDING,
        )


def test_enrollment_cannot_pick_a_favorable_visit_or_replace_an_explicit_refusal(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = rbac_graph
    world = seeded(g, monkeypatch)
    other = second_draft(g, world.chart, g.physician).document.encounter_id
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a, encounter_id=other, informed=True, refused=True
        )
    with signed_in(g.physician) as client:
        url = reverse("ai:chart-assistant", args=(g.clinic_a,))
        for name, value in (
            ("encounter_id", other),
            ("enrollment_id", world.chart.enrollment),
        ):
            refused = client.post(
                url, {"surface": "workspace", "command": "generate", name: str(value)}
            )
            assert refused.status_code == 200
            assert b'data-chart-state="unavailable"' in refused.content
        bodies = []
        for clinic, selected in ((g.clinic_a, uuid4()), (g.clinic_b, other)):
            refused = client.post(
                reverse("ai:chart-assistant", args=(clinic,)),
                {
                    "command": "generate",
                    "surface": "workspace",
                    "encounter_id": str(selected),
                },
            )
            assert refused.status_code == 403
            bodies.append(refused.content)
        assert bodies[0] == bodies[1]
    assert not world.gateway.publications

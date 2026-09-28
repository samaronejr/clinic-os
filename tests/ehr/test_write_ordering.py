"""Every authorization a response depends on is decided before its first write.

The tenant middleware commits any response below 500, so a refusal raised
after a write would keep that write (todo 23's B1). These tests drive the
plan item 27 writers into their late refusals and assert that no domain row
and no audit row other than the fixed ``ehr.access.denied`` record survive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr import services
from apps.ehr.autosave import autosave_draft
from apps.ehr.episodes import open_episode_for_encounter
from apps.ehr.finalization import finalize_version
from apps.ehr.models import (
    DraftEditState,
    DraftSaveReceipt,
    Episode,
    EpisodeEncounter,
)
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError

from auth.stepup_test_support import verified_request
from ehr.test_addenda import clinician, enrollment_of
from ehr.test_autosave import (
    SECTIONS,
    TAB_A,
    as_actor,
    count,
    draft_world,
    open_walk_in,
    registration,
    save,
    stored,
)
from identity.permission_support import owner_context
from renewal.test_encounters import physician_client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
DENIAL = "ehr.access.denied"


def audit_since(graph: RbacGraph, before: int) -> list[str]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(pk__gt=before)
            .order_by("pk")
            .values_list("event_type", flat=True)
        )


def audit_mark(graph: RbacGraph) -> int:
    with owner_context(graph.organization_a):
        last = AuditEvent.objects.order_by("-pk").values_list("pk", flat=True).first()
        return int(last or 0)


def test_workspace_episode_open_refuses_before_writing(rbac_graph: RbacGraph) -> None:
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    enrolled = enrollment_of(
        rbac_graph,
        draft_world(rbac_graph).document.encounter.patient_id,
        rbac_graph.clinic_a,
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        encounter = open_walk_in(rbac_graph, enrolled)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    body = {"encounter_id": encounter.pk, "action": "episode_open"}
    with physician_client(rbac_graph) as client:
        assert client.post(url, {**body, "title": "Primeiro"}).status_code == 302
        # The encounter is already linked: the second open is refused, and
        # the refusal must not leave its episode (or audit row) committed.
        again = client.post(url, {**body, "title": "Órfão"})
        assert again.status_code == 409
    with owner_context(rbac_graph.organization_a):
        assert Episode.objects.count() == 1
        assert EpisodeEncounter.objects.count() == 1
        opened = AuditEvent.objects.filter(event_type="ehr.episode.opened").count()
    assert opened == 1


def test_episode_open_with_encounter_refuses_before_writing(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    before = audit_mark(rbac_graph)
    # The assignee without clinical.write, and a colleague with clinical.write
    # who is not the assignee: both are refused and neither leaves a row.
    for actor in (rbac_graph.physician, colleague):
        with as_actor(rbac_graph, actor), pytest.raises(ClinicalAccessDeniedError):
            open_episode_for_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                title="Recusado",
            )
    assert count(rbac_graph, Episode) == 0
    assert count(rbac_graph, EpisodeEncounter) == 0
    assert set(audit_since(rbac_graph, before)) <= {DENIAL}
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with as_actor(rbac_graph, rbac_graph.physician):
        link = open_episode_for_encounter(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk, title="Asma"
        )
        with pytest.raises(ClinicalConflictError, match="already_linked"):
            open_episode_for_encounter(
                clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk, title="Outro"
            )
    assert count(rbac_graph, Episode) == 1
    assert count(rbac_graph, EpisodeEncounter, pk=link.pk) == 1


def test_care_reader_writes_are_refused_before_the_read_audit(
    rbac_graph: RbacGraph,
) -> None:
    """A care reader passes view_version on a finalized note but may not write.

    The read audit is a write; it must not be appended ahead of the refusal.
    """
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    request = verified_request(rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
        finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    reader = clinician(rbac_graph, "physician", enrolled)
    template = version.template
    with as_actor(rbac_graph, reader):
        # Authoring any note for this patient makes the reader a care reader.
        visit = open_walk_in(rbac_graph, enrolled)
        services.create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=visit.pk,
            template_id=template.pk,
        )
        assert (
            services.view_version(
                clinic_id=rbac_graph.clinic_a, version_id=version.pk
            ).state
            == "finalized"
        )
    before = audit_mark(rbac_graph)
    with as_actor(rbac_graph, reader):
        with pytest.raises(ClinicalAccessDeniedError):
            services.record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                content={**SECTIONS, "plan": "Leitor"},
            )
        with pytest.raises(ClinicalAccessDeniedError):
            autosave_draft(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                editor_command_id=uuid4(),
                editor_session_id=TAB_A,
                sections={**SECTIONS, "plan": "Leitor"},
            )
    assert set(audit_since(rbac_graph, before)) <= {DENIAL}
    assert stored(rbac_graph, version.pk)[0] == 2
    assert count(rbac_graph, DraftSaveReceipt) == 1
    assert count(rbac_graph, DraftEditState, version_id=version.pk) == 1

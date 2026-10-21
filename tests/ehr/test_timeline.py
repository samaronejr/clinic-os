"""Durable refresh, query-time authorization, hidden counts and deterministic races."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ehr.longitudinal import (
    TimelineFilters,
    record_observation,
    timeline,
)
from apps.ehr.models import (
    ClinicalDocumentVersion,
    Encounter,
    Episode,
    EpisodeEncounter,
    Observation,
    SpecialtyTemplate,
    TimelineRefresh,
)
from apps.ehr.note_access import grant_note_access, restrict_note, revoke_note_access
from apps.ehr.services import ClinicalAccessDeniedError, create_draft, view_version
from apps.ehr.tasks import refresh_timeline, refresh_timeline_job
from apps.identity.models import ProfessionalRegistration
from celery.exceptions import OperationalError
from django.db import connections, transaction

from ehr.longitudinal_support import (
    care_reader,
    chart_world,
    drain_projection,
    finalized,
)
from ehr.test_autosave import as_actor
from ehr.test_longitudinal import TEMPERATURE
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_projection_is_after_commit_and_replay_safe(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    drain_projection()
    with as_actor(rbac_graph, rbac_graph.physician):
        observation = record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
        assert refresh_timeline() == 0  # a staff transaction cannot become the worker
        assert (
            timeline(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=world.enrollment,
                filters=TimelineFilters(kind="observation"),
            ).total
            == 0
        )
    assert drain_projection() == 1
    assert drain_projection() == 0
    with as_actor(rbac_graph, rbac_graph.physician):
        page = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(kind="observation"),
        )
        assert [entry.source_id for entry in page.entries] == [observation.pk]
        assert (page.total, page.has_more) == (1, False)
        assert page.entries[0].provenance == "reported_by_patient"


def test_rollback_leaves_no_intent_or_source(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    drain_projection()
    with owner_context(rbac_graph.organization_a):
        before = TimelineRefresh.objects.get(
            clinic_id=rbac_graph.clinic_a, patient_id=world.patient
        ).revision
    with as_actor(rbac_graph, rbac_graph.physician), transaction.atomic():
        record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
        transaction.set_rollback(True)
    assert drain_projection() == 0
    with owner_context(rbac_graph.organization_a):
        assert Observation.objects.count() == 0
        assert (
            TimelineRefresh.objects.get(
                clinic_id=rbac_graph.clinic_a, patient_id=world.patient
            ).revision
            == before
        )


def test_committed_intent_recovers_when_broker_is_unavailable(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = chart_world(rbac_graph)
    drain_projection()
    attempts: list[dict[str, object]] = []

    def unavailable(**options: object) -> None:
        attempts.append(options)
        message = "synthetic broker unavailable"
        raise OperationalError(message)

    monkeypatch.setattr(refresh_timeline_job, "apply_async", unavailable)
    with as_actor(rbac_graph, rbac_graph.physician):
        observation = record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
    assert attempts == [{"retry": False}]
    assert drain_projection() == 1
    with as_actor(rbac_graph, rbac_graph.physician):
        page = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(kind="observation"),
        )
        assert [entry.source_id for entry in page.entries] == [observation.pk]


def test_type_date_episode_and_pagination_filter_authorized_rows(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    start = datetime(2038, 1, 2, tzinfo=UTC)
    with owner_context(rbac_graph.organization_a):
        episode = Episode.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=world.patient,
            title="Episódio sintético",
            opened_by_id=rbac_graph.physician,
        )
        EpisodeEncounter.objects.create(
            organization_id=rbac_graph.organization_a,
            episode=episode,
            encounter=world.version.document.encounter,
            linked_by_id=rbac_graph.physician,
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        observations = [
            record_observation(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=world.enrollment,
                observation=replace(
                    TEMPERATURE, effective_at=start + timedelta(days=i)
                ),
            )
            for i in range(3)
        ]
    assert drain_projection() == 1
    with as_actor(rbac_graph, rbac_graph.physician):
        page = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(
                kind="observation",
                start=start,
                end=start + timedelta(days=2),
                limit=1,
                offset=1,
            ),
        )
        assert (page.total, page.has_more) == (2, False)
        assert [entry.source_id for entry in page.entries] == [observations[0].pk]
        episodic = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(episode_id=episode.pk),
        )
        assert {entry.kind for entry in episodic.entries} == {"encounter", "note"}
        assert episodic.total == 2
        with pytest.raises(ClinicalAccessDeniedError):
            timeline(
                clinic_id=rbac_graph.clinic_a,
                enrollment_id=world.enrollment,
                filters=TimelineFilters(episode_id=uuid4()),
            )


def test_restricted_note_does_not_leak_a_timeline_count(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        template = SpecialtyTemplate.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            key="synthetic-psychiatry",
            version=1,
            title="Psiquiatria sintética",
            prompts=dict.fromkeys(
                ("subjective", "objective", "assessment", "plan"), "Campo sintético"
            ),
        )
        encounter = Encounter.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=world.patient,
            physician_id=rbac_graph.physician,
            unscheduled_reason="documentation_only",
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        psychiatric = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
    source = finalized(rbac_graph, psychiatric)
    reader = care_reader(rbac_graph, world)
    with as_actor(rbac_graph, rbac_graph.physician):
        restrict_note(clinic_id=rbac_graph.clinic_a, version_id=source.pk)
    drain_projection()

    def visible_notes() -> tuple[int, tuple[UUID, ...]]:
        page = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(kind="note"),
        )
        return page.total, tuple(entry.source_id for entry in page.entries)

    with as_actor(rbac_graph, reader):
        before, identifiers = visible_notes()
        assert source.pk not in identifiers
        assert not ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()
        for identifier in (source.pk, uuid4()):
            with pytest.raises(ClinicalAccessDeniedError):
                view_version(clinic_id=rbac_graph.clinic_a, version_id=identifier)
    with as_actor(rbac_graph, rbac_graph.physician):
        grant = grant_note_access(
            clinic_id=rbac_graph.clinic_a, version_id=source.pk, grantee_id=reader
        )
    with as_actor(rbac_graph, reader):
        count, identifiers = visible_notes()
        assert count == before + 1
        assert source.pk in identifiers
        assert (
            view_version(clinic_id=rbac_graph.clinic_a, version_id=source.pk).pk
            == source.pk
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        revoke_note_access(clinic_id=rbac_graph.clinic_a, grant_id=grant.pk)
        # A queued refresh still projects this retained source; it confers no access.
        record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
    drain_projection()
    with as_actor(rbac_graph, reader):
        assert visible_notes()[0] == before
        assert source.pk not in visible_notes()[1]


def test_revocation_after_enqueue_cannot_turn_metadata_into_authority(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        record_observation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            observation=TEMPERATURE,
        )
    with owner_context(rbac_graph.organization_a):
        from django.utils import timezone  # noqa: PLC0415

        ProfessionalRegistration.objects.filter(
            clinic_id=rbac_graph.clinic_a, user_id=rbac_graph.physician
        ).update(revoked_at=timezone.now())
    assert drain_projection() == 1
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(),
        )


def test_concurrent_edits_and_duplicate_workers_preserve_every_source(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    drain_projection()
    ready = Barrier(2, timeout=15)

    def write() -> UUID:
        connections.close_all()
        try:
            with as_actor(rbac_graph, rbac_graph.physician):
                ready.wait()
                return record_observation(
                    clinic_id=rbac_graph.clinic_a,
                    enrollment_id=world.enrollment,
                    observation=TEMPERATURE,
                ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        sources = set(pool.map(lambda _index: write(), range(2)))
    ready = Barrier(2, timeout=15)

    def refresh() -> int:
        connections.close_all()
        try:
            ready.wait()
            return drain_projection()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _index: refresh(), range(2))) == 1
    with as_actor(rbac_graph, rbac_graph.physician):
        page = timeline(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=world.enrollment,
            filters=TimelineFilters(kind="observation"),
        )
        assert {entry.source_id for entry in page.entries} == sources
        assert page.total == 2
    with owner_context(rbac_graph.organization_a):
        queue = TimelineRefresh.objects.get(
            clinic_id=rbac_graph.clinic_a, patient_id=world.patient
        )
        assert queue.revision == queue.refreshed_revision

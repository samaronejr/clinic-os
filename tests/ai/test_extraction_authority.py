"""Allow and deny every extraction adapter on the same real stored subjects."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.ai.models import ExtractionJob
from apps.identity.models import RoleGrant, User, UserClinicRole
from apps.scheduling.models import Appointment, AvailabilityBlock
from django.db import transaction
from django.utils import timezone

from ai.test_extraction_runtime import extracted_job
from identity import nonstaff_census
from identity.nonstaff_differential import _invoke
from identity.nonstaff_extraction_probes import (
    SYMBOLS,
    extraction_calls,
    extraction_probe,
    extraction_probes,
)
from identity.permission_support import owner_context
from identity.test_permission_parity import INVENTORY, agent_password
from scheduling.clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from uuid import UUID

    from identity.nonstaff_differential import DifferentialProbe
    from rbac_fixtures import RbacGraph

__all__ = ("agent_password",)
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


@pytest.mark.parametrize("symbol", sorted(SYMBOLS))
def test_extraction_adapter_decides_by_current_actor_and_permission(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    graph = rbac_graph
    job = extracted_job(graph, monkeypatch)
    for actor, organization, active, allowed in (
        (graph.physician, graph.organization_a, True, True),
        (graph.shared_user, graph.organization_a, True, False),
        (graph.physician, graph.organization_b, True, False),
        (graph.physician, graph.organization_a, False, False),
        (graph.physician, graph.organization_a, True, True),
    ):
        User.objects.filter(pk=graph.physician).update(is_active=active)
        call = extraction_calls(graph, job, actor)[symbol]
        probe = extraction_probe(symbol, call)
        try:
            assert (
                _invoke(probe, User.objects.get(pk=actor), organization) is allowed
            ), (symbol, actor, organization, active)
        finally:
            User.objects.filter(pk=graph.physician).update(is_active=True)
    permissions = (
        ("clinical.read", "clinical.write")
        if symbol == "apps.ai.extraction.review_extraction"
        else ("clinical.read",)
    )
    for permission in permissions:
        with transaction.atomic():
            with owner_context(graph.organization_a):
                RoleGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    role="physician",
                    permission=permission,
                    valid_from=timezone.now() - timedelta(days=1),
                )
            call = extraction_calls(graph, job, graph.physician)[symbol]
            assert not _invoke(
                extraction_probe(symbol, call),
                User.objects.get(pk=graph.physician),
                graph.organization_a,
            ), (symbol, permission)
            transaction.set_rollback(True)


def test_extraction_census_observes_every_delegated_permission_root(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    agent_password: None,
    superuser_database_url: str,
) -> None:
    graph = rbac_graph
    witnesses: list[tuple[UUID, UUID]] = []

    def record_witness(
        subject_graph: RbacGraph,
        actor: UUID,
        patch: pytest.MonkeyPatch,
        *,
        encounter_id: UUID,
    ) -> list[DifferentialProbe]:
        witnesses.append((actor, encounter_id))
        return extraction_probes(subject_graph, actor, patch, encounter_id=encounter_id)

    monkeypatch.setattr(nonstaff_census, "extraction_probes", record_witness)
    with frozen_sql_clocks(superuser_database_url, timezone.now()):
        report = nonstaff_census.run_nonstaff_census(
            INVENTORY["candidates"], graph, monkeypatch
        )
    assert report.observations.keys() >= SYMBOLS
    assert len(witnesses) == 1
    actor, encounter_id = witnesses[0]

    def world_counts() -> tuple[int, ...]:
        with owner_context(graph.organization_a):
            return (
                User.objects.count(),
                UserClinicRole.objects.count(),
                AvailabilityBlock.objects.count(),
                Appointment.objects.count(),
                ExtractionJob.objects.count(),
            )

    before = world_counts()
    with owner_context(graph.organization_a):
        job = ExtractionJob.objects.get(clinic_id=graph.clinic_a)
    for _ in range(2):
        probes = extraction_probes(graph, actor, monkeypatch, encounter_id=encounter_id)
        assert {probe.symbol for probe in probes} == SYMBOLS
    assert world_counts() == before
    with owner_context(graph.organization_a):
        assert ExtractionJob.objects.get(clinic_id=graph.clinic_a).pk == job.pk

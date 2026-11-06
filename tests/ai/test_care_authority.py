"""Allow and deny every delegated care-coordination helper on real subjects."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import RoleGrant, User
from django.db import transaction
from django.utils import timezone

from ai.care_support import care_world, request_and_collect
from identity import nonstaff_census
from identity.nonstaff_care_probes import SYMBOLS, care_calls, care_probe, care_probes
from identity.nonstaff_differential import _invoke
from identity.permission_support import owner_context
from identity.test_permission_parity import INVENTORY, agent_password
from scheduling.clock_support import frozen_sql_clocks

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from identity.nonstaff_differential import DifferentialProbe
    from rbac_fixtures import RbacGraph

__all__ = ("agent_password",)
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})
# The allowed actor of each helper and the permissions its decision rests on.
CLINICIAN = {
    "apps.ai.care_views._clinician": ("automation.propose_clinical",),
    "apps.ai.care_views._context": ("automation.propose_clinical",),
}
STAFF = {
    "apps.ai.care_coordination._care_staff": ("automation.admin", "tasks.assign"),
    "apps.ai.care_views._automation": ("automation.admin",),
    "apps.ai.care_views._care_staff": ("automation.admin", "tasks.assign"),
}


@pytest.mark.parametrize("symbol", sorted(SYMBOLS))
def test_care_helper_decides_by_current_actor_and_permission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    symbol: str,
) -> None:
    assert CLINICIAN.keys() | STAFF.keys() == SYMBOLS
    world = care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    bundle = request_and_collect(world)
    assert bundle.invocation_id is not None
    graph = world.graph
    clinician = symbol in CLINICIAN
    allowed_actor = world.physician if clinician else world.manager
    role = "physician" if clinician else "clinic_manager"

    def decision(actor: UUID, organization: UUID) -> bool:
        probe = care_probe(symbol, care_calls(graph)[symbol])
        return _invoke(probe, User.objects.get(pk=actor), organization)

    assert decision(allowed_actor, graph.organization_a) is True
    assert decision(allowed_actor, graph.organization_b) is False
    # The other actor holds the other half of the page, never this decision.
    other = world.manager if clinician else world.physician
    if symbol != "apps.ai.care_views._context":
        assert decision(other, graph.organization_a) is False
    User.objects.filter(pk=allowed_actor).update(is_active=False)
    try:
        assert decision(allowed_actor, graph.organization_a) is False
    finally:
        User.objects.filter(pk=allowed_actor).update(is_active=True)
    for permission in (CLINICIAN if clinician else STAFF)[symbol]:
        with transaction.atomic():
            with owner_context(graph.organization_a):
                RoleGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    role=role,
                    permission=permission,
                    # care_world promotes every role to the v2 bundle.
                    bundle_version=2,
                    valid_from=timezone.now() - timedelta(days=1),
                )
            assert decision(allowed_actor, graph.organization_a) is False, permission
            transaction.set_rollback(True)


def test_care_census_observes_every_delegated_permission_root(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    agent_password: None,
    superuser_database_url: str,
) -> None:
    witnesses: list[RbacGraph] = []

    def record_witness(graph: RbacGraph) -> list[DifferentialProbe]:
        witnesses.append(graph)
        return care_probes(graph)

    monkeypatch.setattr(nonstaff_census, "care_probes", record_witness)
    with frozen_sql_clocks(superuser_database_url, timezone.now()):
        report = nonstaff_census.run_nonstaff_census(
            INVENTORY["candidates"], rbac_graph, monkeypatch
        )
    assert report.observations.keys() >= SYMBOLS
    assert witnesses == [rbac_graph]
    for symbol in SYMBOLS:
        assert ("function", "clinic_app.has_permission") in report.observations[symbol]

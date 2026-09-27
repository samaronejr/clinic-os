"""Exhaustive worker partitioning must preserve scope and leave no databases."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.fairness import _organization_quotas
from apps.identity.models import User
from django.db import connection

from identity.nonstaff_differential import (
    DifferentialProbe,
    assert_behavioral_classifications,
)
from identity.nonstaff_parallel import Profile, replay_profiles
from identity.nonstaff_states import ROLE_SUBSETS, ReplayScope

if TYPE_CHECKING:
    from identity.nonstaff_differential import DifferentialReport
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_parallel_replay_preserves_every_subset_and_removes_clones(
    rbac_graph: RbacGraph,
) -> None:
    actor = User.objects.create(username="synthetic-parallel-" + uuid4().hex)
    symbol = "apps.core.fairness._organization_quotas"
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
        before = cursor.fetchall()
    report = assert_behavioral_classifications(
        [{"symbol": symbol, "kind": "infrastructure", "signals": []}],
        {
            symbol: [
                DifferentialProbe(
                    symbol, lambda: _organization_quotas(rbac_graph.organization_a)
                )
            ]
        },
        actor=actor,
        scope=ReplayScope(
            rbac_graph.clinic_a,
            rbac_graph.organization_a,
            other_clinic=rbac_graph.clinic_b,
        ),
        parallel=True,
    )
    assert report.subset_count == len(ROLE_SUBSETS)
    assert report.profile_count == 3
    assert report.state_count == len(ROLE_SUBSETS) * 3
    assert report.decisions == {symbol: [(True,) * report.state_count]}
    assert report.serial_profile_seconds is not None
    assert report.parallel_profile_seconds is not None
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
        assert cursor.fetchall() == before


def test_worker_failure_still_removes_every_clone() -> None:
    def broken(_profiles: tuple[Profile, ...]) -> DifferentialReport:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            assert cursor.fetchone()[0].startswith("t6_matrix_")
        message = "synthetic-worker-failure"
        raise RuntimeError(message)

    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
        before = cursor.fetchall()
    with pytest.raises(AssertionError, match="synthetic-worker-failure"):
        replay_profiles([("target", "absent", "target")], broken)
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database ORDER BY datname")
        assert cursor.fetchall() == before

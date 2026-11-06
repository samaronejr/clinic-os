from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from identity.cut1_authority_census import ExecutedProofs
from identity.insurance_authority_census import exercise_insurance_authority
from rbac_fixtures import RbacGraph
from rbac_fixtures import rbac_graph as seed_graph

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.django_db(transaction=True)


def test_insurance_census_executes_each_staff_boundary(
    synthetic_secret_backend: Path,
) -> None:
    symbols = {
        "apps.insurance.access.authority",
        "apps.insurance.appointment_coverage.booking_coverage",
        "apps.insurance.appointment_coverage.validate_insured_booking",
        "apps.insurance.guides.draft_guide",
        "apps.insurance.memberships.backfill_membership_payers",
    }
    proofs = ExecutedProofs(symbols)

    def graph() -> RbacGraph:
        result = seed_graph._get_wrapped_function()(synthetic_secret_backend)
        assert isinstance(result, RbacGraph)
        return result

    exercise_insurance_authority(proofs, graph)
    assert proofs.executed() == {symbol + "#cut1" for symbol in symbols}

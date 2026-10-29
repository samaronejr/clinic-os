"""Every expression relation must enter the clock closure, not an incidental alias."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .clock_catalog import _build_inventory, live_clock_inventory
from .clock_expression_probes import POLICY, POLICY_FUNCTION, RULE
from .test_residual_clock_probes import RESTORATION, installed_probe
from .test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from .clock_residual_probes import Probe

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


@pytest.mark.parametrize(
    "probe", [RULE, POLICY, POLICY_FUNCTION], ids=["rule", "policy", "policy-function"]
)
def test_expression_relation_is_followed(
    probe: Probe,
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    with installed_probe(superuser_database_url, probe, request) as owner:
        if probe is POLICY:
            for at, expected in [("2999-01-01", 0), ("2001-01-01", 1)]:
                owner.execute("UPDATE r9.zq_gate SET ends_at=%s", [at])
                with owner.transaction():
                    owner.execute("SET LOCAL ROLE clinic_owner")
                    assert owner.execute(
                        "SELECT count(*) FROM r9.zq_rows"
                    ).fetchone() == (expected,)
        observed = live_clock_inventory()
        assert observed == _build_inventory()
        if probe is POLICY:
            assert (
                observed["policy:r9.zq_gate.zq_pg"]["direct"]["pg_catalog.now()"] == 1
            )
            assert (
                "policy:r9.zq_gate.zq_pg" in observed["policy:r9.zq_rows.zq_pr"]["via"]
            )
        else:
            assert (
                observed["default:r9.zq_journal.at"]["direct"][
                    "pg_catalog.clock_timestamp()"
                ]
                == 1
            )
            edge = (
                "rule:r9.zq_t.zq_also" if probe is RULE else "function:r9.zq_helper()"
            )
            assert "default:r9.zq_journal.at" in observed[edge]["via"]
        with pytest.raises(AssertionError):
            assert_inventory()
        request.node.stash[RESTORATION]["census_rejected"] = "true"

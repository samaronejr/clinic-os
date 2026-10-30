"""Each exact native identity remains opaque and loses approval without its pin."""

from dataclasses import asdict

import pytest

from scheduling import clock_catalog
from scheduling.clock_catalog import functions, opaque_marker, opaque_policy
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

pytestmark = pytest.mark.django_db(transaction=True)
IDENTITIES = (
    "pg_catalog.pg_trigger_depth()",
    "pg_catalog.oideq(oid, oid)",
    "pg_catalog.sum(bigint)",
    "pg_catalog.sum(double precision)",
    "pg_catalog.sum(integer)",
    "pg_catalog.sum(interval)",
    "pg_catalog.sum(money)",
    "pg_catalog.sum(numeric)",
    "pg_catalog.sum(real)",
    "pg_catalog.sum(smallint)",
)


@pytest.mark.parametrize("identity", IDENTITIES)
def test_removing_one_retention_native_pin_restores_census_refusal(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    procedure = next(p for p in functions().values() if p.identity == identity)
    policy = opaque_policy()
    for field in (
        "language",
        "implementation",
        "library",
        "extension",
        "returns",
        "volatility",
    ):
        assert policy[identity][field] == asdict(procedure)[field]
    assert opaque_marker(procedure) == "opaque:reviewed:" + identity
    incomplete = {key: value for key, value in policy.items() if key != identity}
    monkeypatch.setattr("scheduling.clock_catalog.opaque_policy", lambda: incomplete)
    assert clock_catalog.opaque_marker(procedure) == "opaque:unreviewed:" + identity
    with pytest.raises(AssertionError):
        assert_inventory()

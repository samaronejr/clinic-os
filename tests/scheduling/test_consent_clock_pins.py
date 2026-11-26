"""Each opaque PostgreSQL dependency reached by F5 has its own reviewed identity."""

from dataclasses import asdict

import pytest

from scheduling import clock_catalog
from scheduling.clock_catalog import functions, live_clock_inventory, opaque_policy
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)
from scheduling.test_scheduling_clock_inventory import (
    test_reachable_opaque_functions_have_exact_reviewed_identities as assert_review,
)

pytestmark = pytest.mark.django_db(transaction=True)
F5_NATIVE = (
    'pg_catalog.jsonb_object_agg("any", "any")',
    "pg_catalog.pg_try_advisory_xact_lock_shared(bigint)",
    "pg_catalog.pg_try_advisory_xact_lock_shared(integer, integer)",
    "pg_catalog.split_part(text, text, integer)",
)


@pytest.mark.parametrize("identity", F5_NATIVE)
def test_removing_one_consent_clock_pin_is_detected(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    procedure = next(p for p in functions().values() if p.identity == identity)
    policy = opaque_policy()
    assert policy[identity]["reason"]
    assert all(
        policy[identity][field] == asdict(procedure)[field]
        for field in (
            "language",
            "implementation",
            "library",
            "extension",
            "returns",
            "volatility",
        )
    )
    key = "function:" + identity
    assert live_clock_inventory()[key]["direct"] == {"opaque:reviewed:" + identity: 1}
    without = dict(policy)
    del without[identity]
    monkeypatch.setattr(clock_catalog, "opaque_policy", lambda: without)
    assert live_clock_inventory()[key]["direct"] == {"opaque:unreviewed:" + identity: 1}
    with pytest.raises(AssertionError):
        assert_review()
    with pytest.raises(AssertionError):
        assert_inventory()

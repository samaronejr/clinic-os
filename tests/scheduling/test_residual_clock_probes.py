"""Execute the reviewer's three escapes and variants against the real gates."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
import pytest

from scheduling.clock_boundary import application_boundary
from scheduling.clock_catalog import functions, live_clock_inventory
from scheduling.clock_probe_catalog import catalog_sha256
from scheduling.clock_residual_probes import EVENTS, PROBES, Probe
from scheduling.test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any

pytestmark = pytest.mark.django_db(transaction=True)
RESTORATION = pytest.StashKey[dict[str, str]]()


@contextmanager
def installed_probe(
    database_url: str, probe: Probe, request: pytest.FixtureRequest
) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    before = live_clock_inventory()
    fingerprint = catalog_sha256()
    receipt = {
        "catalog_before_sha256": fingerprint,
        "planted_sql_sha256": hashlib.sha256(probe.install.encode()).hexdigest(),
    }
    request.node.stash[RESTORATION] = receipt
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute(probe.install.encode())
        try:
            actual = owner.execute(probe.runtime.encode()).fetchone()
            receipt["runtime_super_result"] = repr(actual)
            assert actual == probe.expected
            if probe.owner_expected is not None:
                with owner.transaction():
                    owner.execute("SET LOCAL ROLE clinic_owner")
                    actual = owner.execute(probe.runtime.encode()).fetchone()
                    receipt["runtime_owner_result"] = repr(actual)
                    assert actual == probe.owner_expected
            yield owner
        finally:
            owner.execute(probe.remove.encode())
            restored = catalog_sha256()
            receipt["catalog_after_sha256"] = restored
            assert restored == fingerprint
            assert live_clock_inventory() == before
            assert_inventory()


@pytest.mark.parametrize("name", PROBES)
def test_residual_reader_is_censused(
    superuser_database_url: str,
    name: str,
    request: pytest.FixtureRequest,
) -> None:
    probe = PROBES[name]
    before = live_clock_inventory()
    with installed_probe(superuser_database_url, probe, request):
        observed = live_clock_inventory()
        assert observed != before
        if probe.blocker == "B3-VIEW":
            assert observed["policy:r5.hol.p"]["direct"]["pg_catalog.now()"] == 1
        elif name.startswith("literal-"):
            assert any(
                key.startswith("unresolved-temporal-input")
                for node in observed.values()
                for key in node["direct"]
            )
        else:
            stamp = "MixedStamp" if "variant" in name else "stamp"
            procedure = next(
                function
                for function in functions().values()
                if function.schema == "r5" and function.name == stamp
            )
            key = "function:" + procedure.identity
            assert observed[key]["direct"]["pg_catalog.now()"] == 1
            assert key in observed["function:r5.h()"]["via"]
        with pytest.raises(AssertionError):
            assert_inventory()
        request.node.stash[RESTORATION]["census_rejected"] = "true"


@pytest.mark.parametrize("name", EVENTS)
def test_event_trigger_is_refused_by_source_guard(
    superuser_database_url: str,
    name: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    probe = Probe(
        "B5-EVENT-TRIGGER",
        EVENTS[name],
        "DROP EVENT TRIGGER r5_evt; DROP SCHEMA r5 CASCADE;",
        "SELECT evtenabled='O' FROM pg_event_trigger WHERE evtname='r5_evt'",
    )
    apps = tmp_path / "apps"
    path = apps / "outside_scheduling" / "migrations" / "event.py"
    path.parent.mkdir(parents=True)
    path.write_text("SQL = " + repr(probe.install) + "\n")
    with installed_probe(superuser_database_url, probe, request) as owner:
        owner.execute("SET track_functions='all'")
        with owner.transaction():
            owner.execute("CREATE TABLE r5.firing_probe (value integer)")
            owner.execute("DROP TABLE r5.firing_probe")
            reached = owner.execute(
                "SELECT calls FROM pg_stat_xact_user_functions "
                "WHERE funcid='r5.evt()'::regprocedure"
            ).fetchone()
            assert reached is not None
            assert reached[0] >= 1
        assert (
            "event-trigger-ddl"
            in application_boundary(apps)[str(path.relative_to(apps))]
        )
        request.node.stash[RESTORATION]["event_trigger_calls"] = str(reached[0])
        request.node.stash[RESTORATION]["guard_rejected"] = "true"

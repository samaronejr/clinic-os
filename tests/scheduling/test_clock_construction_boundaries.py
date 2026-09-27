"""Round-6 catalog/value rules: exact escapes, variants and occurrence counts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest

from .clock_boundary import (
    application_boundary,
    repository_event_trigger_boundary,
)
from .clock_catalog import live_clock_inventory
from .clock_construction_probes import EVENTS, PROBES, SOURCES
from .clock_probe_catalog import catalog_sha256
from .clock_residual_probes import Probe
from .test_residual_clock_probes import RESTORATION, installed_probe
from .test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


@pytest.mark.parametrize("name", PROBES)
def test_construction_probe_is_censused(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    before = live_clock_inventory()
    with installed_probe(superuser_database_url, PROBES[name], request):
        observed = live_clock_inventory()
        if name == "text-variant-prefix-message":
            # Round-7's whole-word rule intentionally permits the word "nowhere".
            assert observed == before
            request.node.stash[RESTORATION]["whole_word_negative_control"] = "true"
            return
        assert observed != before
        if name.startswith("text-"):
            assert (
                observed["function:clinic_app.scheduling_r6_root()"]["direct"][
                    "unresolved-temporal-input"
                ]
                == 1
            )
        elif name == "relation-unresolved-identifier":
            assert any(
                "unresolved-identifier" in key
                for node in observed.values()
                for key in node["direct"]
            )
        else:
            assert any(
                node["direct"].get("pg_catalog.now()") == 1
                for key, node in observed.items()
                if key.startswith("policy:r6.")
            )
        with pytest.raises(AssertionError):
            assert_inventory()
        request.node.stash[RESTORATION]["census_rejected"] = "true"


@pytest.mark.parametrize("name", EVENTS)
def test_event_catalog_rejects_every_enable_path(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
    name: str,
) -> None:
    install, enable, state, unmember = EVENTS[name]
    probe = Probe(
        "B5-EVENT",
        install,
        unmember + "DROP EVENT TRIGGER r6_evt; DROP SCHEMA r6 CASCADE",
        "SELECT evtenabled FROM pg_event_trigger WHERE evtname='r6_evt'",
        ("D",),
    )
    with installed_probe(superuser_database_url, probe, request) as owner:
        # Disabled triggers are forbidden too; source spelling is not authority.
        with pytest.raises(AssertionError, match=r"event.trigger"):
            live_clock_inventory()
        owner.execute(enable.encode())
        assert owner.execute(probe.runtime.encode()).fetchone() == (state,)
        with pytest.raises(AssertionError, match=r"event.trigger"):
            assert_inventory()
        request.node.stash[RESTORATION]["census_rejected"] = "true"


@pytest.mark.parametrize("name", SOURCES)
def test_cross_check_rejects_unresolved_runsql_and_unlisted_files(
    tmp_path: Path,
    name: str,
) -> None:
    relative, source = SOURCES[name]
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    assert repository_event_trigger_boundary(tmp_path)


@pytest.mark.parametrize("encoding", ["utf-16", "latin-1"])
def test_source_cross_check_reads_non_utf8_text(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "event.arbitrary"
    path.write_bytes(
        ("-- caf\u00e9\n" + next(iter(EVENTS.values()))[0]).encode(encoding)
    )
    assert repository_event_trigger_boundary(tmp_path) == {
        "event.arbitrary": ["event-trigger-ddl"],
    }


@pytest.mark.parametrize(
    "value", ["now", "  ToDaY", "tomorrow", "YESTERDAY", "nowhere"]
)
def test_python_scheduling_bound_literals_fail_closed(
    tmp_path: Path, value: str
) -> None:
    apps = tmp_path / "apps"
    path = apps / "scheduling" / "probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        'cursor.execute("SELECT clinic_app.helper(%s)", [' + repr(value) + "])"
    )
    if value == "nowhere":
        assert application_boundary(apps) == {}
    else:
        assert application_boundary(apps)


def test_additional_literals_change_controlled_inventory(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    fingerprint = catalog_sha256()
    before = live_clock_inventory()
    request.node.stash[RESTORATION] = {"catalog_before_sha256": fingerprint}
    key = "function:clinic_app.scheduling_generated_block_guard()"
    count = before[key]["direct"].get("unresolved-temporal-input", 0)
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        row = owner.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.scheduling_generated_block_guard()'::regprocedure)"
        ).fetchone()
        assert row is not None
        original = str(row[0])
        try:
            for added in (1, 2):
                definition = original.replace(
                    "BEGIN", "BEGIN\n" + "PERFORM 'now'::timestamptz;\n" * added, 1
                )
                owner.execute(definition.encode())
                observed = live_clock_inventory()
                assert (
                    observed[key]["direct"]["unresolved-temporal-input"]
                    == count + added
                )
                with pytest.raises(AssertionError):
                    assert_inventory()
        finally:
            owner.execute(original.encode())
    restored = catalog_sha256()
    request.node.stash[RESTORATION]["catalog_after_sha256"] = restored
    assert restored == fingerprint
    assert live_clock_inventory() == before
    assert_inventory()

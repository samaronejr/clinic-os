"""The staged SC-17 seed cannot silently skip a domain the candidate adds."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import psycopg
import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from ops.testing import migration_upgrade_domains as domains
from ops.testing import migration_upgrade_stages as stages
from ops.testing.migration_upgrade_witness import super_dsn
from psycopg import sql

if TYPE_CHECKING:
    from pathlib import Path

Node = tuple[str, str]


def _loader() -> MigrationLoader:
    return MigrationLoader(None, ignore_no_migrations=True)


def _base_without(loader: MigrationLoader, app: str) -> set[Node]:
    """Every migration except ``app``'s and everything that depends on them."""
    removed = {node for node in loader.graph.nodes if node[0] == app}
    for node in list(removed):
        removed |= set(loader.graph.backwards_plan(node))
    return set(loader.graph.nodes) - removed


def _record(path: Path, base: set[Node], rows: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": stages.SCHEMA_VERSION,
                "base_nodes": sorted(base),
                "organization_id": "10000000-0000-4000-8000-000000000020",
                "owner_id": "30000000-0000-4000-8000-000000000020",
                "physician_id": "40000000-0000-4000-8000-000000000020",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_blank_database_is_refused() -> None:
    with pytest.raises(RuntimeError, match="blank database"):
        stages.new_models(_loader(), [])


def test_fully_migrated_base_adds_no_model() -> None:
    loader = _loader()
    assert stages.new_models(loader, loader.graph.nodes) == {}


def test_every_registry_entry_names_a_concrete_leaf_model() -> None:
    for app, entries in domains.DOMAIN_SEEDS.items():
        for name, seed in entries.items():
            model = apps.get_model(app, name)
            assert model._meta.managed
            assert not model._meta.proxy
            for label, _support in seed.support:
                apps.get_model(label)


def test_graph_derived_domain_without_rows_fails(tmp_path: Path) -> None:
    loader = _loader()
    base = _base_without(loader, "crm")
    required = stages.new_models(loader, base)
    assert {label for label, app in required.items() if app == "crm"} == {
        f"crm.{name}" for name in domains.CRM
    }
    record = json.loads(_record(tmp_path / "seed.json", base, []).read_bytes())
    assert stages.coverage_failures(record) == [
        f"domain {app}: new model {label} has no seeded row"
        for label, app in sorted(required.items())
    ]


def test_one_seeded_model_does_not_cover_its_domain(tmp_path: Path) -> None:
    loader = _loader()
    base = _base_without(loader, "crm")
    row: dict[str, object] = {"model": "crm.Lead", "pk": "x"}
    record = json.loads(_record(tmp_path / "seed.json", base, [row]).read_bytes())
    failures = [line for line in stages.coverage_failures(record) if "crm." in line]
    assert "domain crm: new model crm.Campaign has no seeded row" in failures
    assert not any("crm.Lead has no seeded row" in line for line in failures)


def test_unknown_record_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "seed.json"
    path.write_text("{}", encoding="utf-8")
    assert stages.verify_stages(path) == [
        "domain seed record is missing or has an unknown schema"
    ]


# A table every migrated database guards with user triggers; the tests below
# disable one inside the test transaction only (pytest-django rolls it back).
POSTURE_TABLE = "scheduling_appointment"


def _first_user_trigger() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid=%s::regclass "
            "AND NOT tgisinternal ORDER BY tgname LIMIT 1",
            ["clinic_app." + POSTURE_TABLE],
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


@pytest.mark.django_db
def test_live_trigger_posture_passes_on_a_migrated_database() -> None:
    with connection.cursor() as cursor:
        assert stages.disabled_triggers(cursor) == set()
        stages._require_live_triggers(cursor)


@pytest.mark.django_db
def test_any_disabled_user_trigger_fails_the_stage() -> None:
    name = _first_user_trigger()
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER TABLE clinic_app.{} DISABLE TRIGGER {}").format(
                sql.Identifier(POSTURE_TABLE), sql.Identifier(name)
            )
        )
        assert stages.disabled_triggers(cursor) == {(POSTURE_TABLE, name)}
        with pytest.raises(RuntimeError, match=name):
            stages._require_live_triggers(cursor)


@pytest.mark.django_db
def test_replica_replication_role_fails_the_stage() -> None:
    # Only a superuser may switch the replication role (which silences every
    # ordinary trigger): run the posture check on such a session of the test
    # database, inside a transaction that is rolled back.
    target = connection.settings_dict["NAME"]
    with psycopg.connect(super_dsn(), dbname=target) as raw, raw.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        with pytest.raises(RuntimeError, match="session_replication_role"):
            stages._require_live_triggers(cursor)  # type: ignore[arg-type]
        raw.rollback()


@pytest.mark.django_db
def test_insert_guard_discovery_reads_the_live_catalog() -> None:
    with connection.cursor() as cursor:
        guards = stages._insert_guards(cursor, POSTURE_TABLE)
    # The appointment lifecycle guard fires BEFORE INSERT FOR EACH ROW; its
    # AFTER receipt trigger does not guard an insert.
    assert "scheduling_appointment_guard" in guards
    assert "scheduling_appointment_lifecycle_receipt" not in guards


@pytest.mark.django_db
@pytest.mark.parametrize("name", sorted(domains.LEAF_INVARIANTS))
def test_leaf_invariant_runs_against_the_migrated_schema(name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            domains.LEAF_INVARIANTS[name],
            {"organization": "10000000-0000-4000-8000-000000000099"},
        )
        assert cursor.fetchone() == (0,)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "model": "crm.Lead",
        "pk": "x",
        "adopted": False,
        "history": [{}],
        "writes": [{"op": "insert", "role": "clinic_owner", "guards": ["crm_binding"]}],
    }
    return row | overrides


def test_well_formed_write_log_passes() -> None:
    transition = {"op": "update", "role": "clinic_owner", "columns": ["state"]}
    row = _row(
        history=[{}, {}, {}],
        writes=[*cast("list[object]", _row()["writes"]), transition, {"op": "product"}],
    )
    assert (
        stages.provenance_failures(
            [row, _row(adopted=True, writes=[{"op": "product"}])]
        )
        == []
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"history": [{}, {}]}, "2 states but 1 logged writes"),
        (
            {"writes": [{"op": "product"}]},
            "product-written first state without adoption",
        ),
        (
            {"writes": [{"op": "insert", "role": "postgres", "guards": ["g"]}]},
            "is not a product write",
        ),
        (
            {
                "history": [{}, {}],
                "writes": [
                    {"op": "insert", "role": "clinic_app", "guards": ["g"]},
                    {"op": "insert", "role": "clinic_app", "guards": ["g"]},
                ],
            },
            "inserted twice",
        ),
    ],
)
def test_unreachable_history_fails_the_leaf(
    overrides: dict[str, object], reason: str
) -> None:
    failures = stages.provenance_failures([_row(**overrides)])
    assert len(failures) == 1
    assert reason in failures[0]


def test_secret_columns_are_recorded_only_as_digests() -> None:
    key = bytes(range(20))
    recorded = stages._recorded("identity.TotpSecret", "secret", key)
    assert isinstance(recorded, str)
    assert recorded.startswith("sha256:")
    assert key.hex() not in recorded


@pytest.mark.parametrize(
    "column",
    [
        "client_secret",
        "otp_seed",
        "api_key",
        "access_token",
        "refresh_token",
        "signing_key",
    ],
)
def test_unclassified_secret_column_fails_closed(column: str) -> None:
    with pytest.raises(LookupError, match="not classified"):
        stages._recorded("interop.ApiClient", column, "synthetic-value")
    assert stages._recorded("interop.ApiClientSecret", "secret_digest", "a" * 64)


def test_historically_protected_plain_leaf_column_is_still_checked() -> None:
    row: dict[str, object] = {
        "values": {"name": "Synthetic Prospect", "email": ""},
        "protected": ["name", "email"],
        "encrypted": [],
    }
    checked, empty = stages.protected_columns(row, {"name", "email"})
    assert (checked, empty) == (["name"], 1)
    assert stages.envelope_failures(
        "crm.Lead",
        "name",
        "Synthetic Prospect",
        "Synthetic Prospect",
        leaf_protected=False,
    ) == ["crm.Lead.name is not stored as a ciphertext envelope"]


def test_envelope_must_hide_plaintext_and_stay_protected() -> None:
    plain = b"\x01Synthetic Prospect"
    assert stages.envelope_failures(
        "crm.Lead", "name", plain, "Synthetic Prospect", leaf_protected=True
    ) == ["crm.Lead.name stores readable plaintext"]
    assert stages.envelope_failures(
        "crm.Lead", "name", b"\x00" * 40, "Synthetic Prospect", leaf_protected=False
    ) == ["crm.Lead.name lost its envelope protection at the leaf"]
    assert not stages.envelope_failures(
        "crm.Lead", "name", b"\x00" * 40, "Synthetic Prospect", leaf_protected=True
    )

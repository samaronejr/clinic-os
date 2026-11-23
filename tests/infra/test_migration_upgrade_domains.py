"""The staged SC-17 seed cannot silently skip a domain the candidate adds."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader
from ops.testing import migration_upgrade_domains as domains
from ops.testing import migration_upgrade_stages as stages

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from django.db.backends.utils import CursorWrapper

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


class _Catalog:
    """A cursor over one fixed pg_trigger answer; records the DDL it gets."""

    def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
        self.rows = list(rows)
        self.executed: list[str] = []

    def execute(self, statement: object, _params: object = None) -> None:
        self.executed.append(str(statement))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


def _cursor(rows: Sequence[tuple[object, ...]]) -> CursorWrapper:
    return cast("CursorWrapper", _Catalog(rows))


BEFORE_ROW_INSERT = 1 | 2 | 4
AFTER_ROW_INSERT = 1 | 4


def test_only_a_before_row_insert_guard_can_be_suspended() -> None:
    fake = _Catalog([(BEFORE_ROW_INSERT, "O", False)])
    guard = cast("CursorWrapper", fake)
    stages._suspend_guard(
        guard, "identity_breakglassgrant", "enterprise_guard", "INSERT"
    )
    assert "DISABLE TRIGGER" in fake.executed[-1]
    for row in (
        (AFTER_ROW_INSERT, "O", True),  # enterprise_grant_complete
        (AFTER_ROW_INSERT, "O", False),  # a derived AFTER write
        (BEFORE_ROW_INSERT, "D", False),  # already off before the stage
    ):
        with pytest.raises(RuntimeError):
            stages._suspend_guard(_cursor([row]), "t", "x", "INSERT")
    with pytest.raises(RuntimeError, match="does not exist"):
        stages._suspend_guard(_cursor([]), "t", "x", "INSERT")


def test_any_trigger_off_outside_the_allowlist_fails_the_stage() -> None:
    allowed = {("identity_breakglassgrant", "enterprise_guard")}
    stages._require_no_disabled(_cursor(sorted(allowed)), allowed)
    extra: list[tuple[object, ...]] = [
        *allowed,
        ("identity_breakglassgrant", "enterprise_grant_complete"),
    ]
    with pytest.raises(RuntimeError, match="enterprise_grant_complete"):
        stages._require_no_disabled(_cursor(extra), allowed)


def test_registry_allowlists_name_one_guard_per_loaded_model() -> None:
    loaded = {
        f"{app}.{name}"
        for app, entries in domains.DOMAIN_SEEDS.items()
        for name in entries
    }
    loaded |= {
        support
        for entries in domains.DOMAIN_SEEDS.values()
        for seed in entries.values()
        for support, _helper in seed.support
    }
    assert set(domains.GUARDED_TRIGGERS) <= loaded
    assert all(len(names) == 1 for names in domains.GUARDED_TRIGGERS.values())
    assert all(
        "%(organization)s" in query for query in domains.LEAF_INVARIANTS.values()
    )


def test_secret_columns_are_recorded_only_as_digests() -> None:
    key = bytes(range(20))
    recorded = stages._recorded("identity.TotpSecret", "secret", key)
    assert isinstance(recorded, str)
    assert recorded.startswith("sha256:")
    assert key.hex() not in recorded


def test_unclassified_secret_column_fails_closed() -> None:
    with pytest.raises(LookupError, match="not classified"):
        stages._recorded("interop.ApiClient", "client_secret", b"synthetic")
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

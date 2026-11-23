"""The staged SC-17 seed cannot silently skip a domain the candidate adds."""

import json
from pathlib import Path

import pytest
from django.apps import apps
from django.db.migrations.loader import MigrationLoader
from ops.testing import migration_upgrade_domains as domains
from ops.testing import migration_upgrade_stages as stages

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
    failures = stages.verify_stages(_record(tmp_path / "seed.json", base, []))
    assert failures == [
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

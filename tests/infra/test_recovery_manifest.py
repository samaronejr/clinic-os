"""Every relation in the migrated catalog is classified for logical recovery.

The reference set is derived from the live catalog (``pg_class``,
``pg_namespace`` and ``pg_depend`` for sequence ownership), never from a hand
list: a table, partition, sequence, view, materialized view, foreign table or
composite type that nobody classified in ``ops/testing/restore_contract.py``
fails here, naming the relation, before it can break ``make
restore-rehearsal``. Only indexes are skipped; they hold no data of their own
and are rebuilt with their table. Anything outside ``clinic_app`` fails too,
because ``pg_restore --schema=clinic_app`` never restores it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from django.db import connection, transaction
from ops.testing import restore_contract
from ops.testing.restore_queries import SOURCE_SCOPE_SQL

pytestmark = pytest.mark.django_db(transaction=True)

APP_SCHEMA = "clinic_app"
CLASSIFY = (
    "classify it in ops/testing/restore_contract.py (DOMAIN_RELATIONS, "
    "SEQUENCE_TARGETS, REQUIRED_EMPTY, or TARGET_OWNED_RELATIONS with a reason)"
)
CATALOG_SQL = """
SELECT namespace.nspname, class.relname, class.relkind::text,
       class.relispartition, owner.relname, attribute.attname
FROM pg_catalog.pg_class AS class
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = class.relnamespace
LEFT JOIN pg_catalog.pg_depend AS dependency
  ON class.relkind = 'S'
 AND dependency.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
 AND dependency.objid = class.oid
 AND dependency.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
 AND dependency.deptype IN ('a', 'i')
LEFT JOIN pg_catalog.pg_class AS owner ON owner.oid = dependency.refobjid
LEFT JOIN pg_catalog.pg_attribute AS attribute
  ON attribute.attrelid = dependency.refobjid
 AND attribute.attnum = dependency.refobjsubid
WHERE namespace.nspname <> 'information_schema'
  AND namespace.nspname NOT LIKE 'pg\\_%'
  AND class.relkind NOT IN ('i', 'I')
ORDER BY namespace.nspname, class.relname
"""


@dataclass(frozen=True, slots=True)
class Relation:
    """One catalog relation plus, for sequences, its owning table column."""

    schema: str
    name: str
    kind: str
    partition: bool
    owner_table: str | None = None
    owner_column: str | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    """The recovery classification under test; defaults to the contract."""

    domain: tuple[str, ...] = restore_contract.DOMAIN_RELATIONS
    sequences: tuple[tuple[str, tuple[str, str]], ...] = tuple(
        restore_contract.SEQUENCE_TARGETS.items()
    )
    excluded: tuple[str, ...] = restore_contract.EXCLUDED_RELATIONS
    prefixes: tuple[str, ...] = restore_contract.EXCLUDED_PREFIXES


def catalog_relations() -> tuple[Relation, ...]:
    with connection.cursor() as cursor:
        cursor.execute(CATALOG_SQL)
        rows = cursor.fetchall()
    return tuple(Relation(*row) for row in rows)


def _excluded(name: str, manifest: Manifest) -> bool:
    return name in manifest.excluded or name.startswith(manifest.prefixes)


def _table_violation(relation: Relation, manifest: Manifest) -> str | None:
    if relation.name in manifest.domain:
        if relation.kind == "p":
            return (
                f"partitioned table {relation.name!r} carries no TABLE DATA; "
                "list each partition in DOMAIN_RELATIONS of "
                "ops/testing/restore_contract.py instead"
            )
        return None
    if _excluded(relation.name, manifest):
        return None
    what = "partition" if relation.partition else "table"
    return f"unclassified {what} {relation.name!r}: {CLASSIFY}"


def _sequence_violation(relation: Relation, manifest: Manifest) -> str | None:
    targets = dict(manifest.sequences)
    owner = (relation.owner_table, relation.owner_column)
    if relation.name in targets:
        if targets[relation.name] != owner or owner[0] not in manifest.domain:
            return (
                f"sequence {relation.name!r} maps to {targets[relation.name]} "
                f"but the catalog owner is {owner}: fix SEQUENCE_TARGETS in "
                "ops/testing/restore_contract.py"
            )
        return None
    if relation.owner_table is None:
        return f"unclassified free-standing sequence {relation.name!r}: {CLASSIFY}"
    if relation.owner_table in manifest.domain:
        return (
            f"sequence {relation.name!r} is owned by restored table "
            f"{relation.owner_table!r}; restored rows would collide with its "
            "next value: add it to SEQUENCE_TARGETS in "
            "ops/testing/restore_contract.py"
        )
    if _excluded(relation.owner_table, manifest):
        return None
    return (
        f"unclassified sequence {relation.name!r} owned by unclassified "
        f"{relation.owner_table!r}: {CLASSIFY}"
    )


def unclassified(
    relations: tuple[Relation, ...], manifest: Manifest | None = None
) -> list[tuple[str, str]]:
    """Return ``(relation, message)`` for each relation left unclassified."""
    manifest = manifest or Manifest()
    violations: list[tuple[str, str]] = []
    for relation in relations:
        if relation.schema != APP_SCHEMA:
            violations.append(
                (
                    relation.name,
                    f"relation {relation.schema}.{relation.name} is outside "
                    f"{APP_SCHEMA}; pg_restore --schema={APP_SCHEMA} never "
                    "restores it",
                )
            )
            continue
        if relation.kind in {"r", "p"}:
            violation = _table_violation(relation, manifest)
        elif relation.kind == "S":
            violation = _sequence_violation(relation, manifest)
        elif relation.name in manifest.excluded:
            violation = None
        else:
            violation = (
                f"unclassified relation {relation.name!r} of kind "
                f"{relation.kind!r}: {CLASSIFY}"
            )
        if violation is not None:
            violations.append((relation.name, violation))
    return violations


def stale_entries(
    relations: tuple[Relation, ...], manifest: Manifest | None = None
) -> list[str]:
    """Return manifest names the migrated catalog does not contain."""
    manifest = manifest or Manifest()
    present = {relation.name for relation in relations if relation.schema == APP_SCHEMA}
    named = {*manifest.domain, *dict(manifest.sequences), *manifest.excluded}
    return sorted(named - present)


def _runtime_unexpected_relations() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(SOURCE_SCOPE_SQL)
        row = cursor.fetchone()
    assert row is not None
    value = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    unexpected = value["unexpected_domain_relations"]
    assert isinstance(unexpected, list)
    return unexpected


def test_every_catalog_relation_is_classified_for_recovery() -> None:
    # Given: the relation set derived from the live migrated catalog
    relations = catalog_relations()
    assert {relation.kind for relation in relations} >= {"r", "S"}

    # When: each relation is looked up in the fixed recovery manifest
    violations = unclassified(relations)

    # Then: nothing is left for the rehearsal to refuse or silently drop
    assert violations == [], "\n".join(message for _, message in violations)


def test_recovery_manifest_names_only_existing_relations() -> None:
    relations = catalog_relations()
    manifest = Manifest()

    assert stale_entries(relations) == []
    assert len(set(manifest.domain)) == len(manifest.domain)
    assert set(manifest.domain).isdisjoint(manifest.excluded)
    assert all(restore_contract.TARGET_OWNED_RELATIONS.values())
    assert set(restore_contract.TARGET_SEEDED_RELATIONS) <= set(
        restore_contract.TARGET_OWNED_RELATIONS
    )


def test_runtime_source_scope_accepts_the_migrated_catalog() -> None:
    # The rehearsal's own unexpected-relation gate agrees with the census.
    assert _runtime_unexpected_relations() == []


def _flagged(relations: tuple[Relation, ...], manifest: Manifest) -> set[str]:
    return {name for name, _ in unclassified(relations, manifest)}


def test_removing_any_manifest_entry_is_caught() -> None:
    relations = catalog_relations()
    manifest = Manifest()
    for name in manifest.domain:
        domain = tuple(entry for entry in manifest.domain if entry != name)
        assert name in _flagged(relations, Manifest(domain=domain)), name
    for name in manifest.excluded:
        excluded = tuple(entry for entry in manifest.excluded if entry != name)
        assert name in _flagged(relations, Manifest(excluded=excluded)), name
    for name, _ in manifest.sequences:
        sequences = tuple(item for item in manifest.sequences if item[0] != name)
        assert name in _flagged(relations, Manifest(sequences=sequences)), name


def test_new_unclassified_relations_fail_by_name() -> None:
    # Given: throwaway relations of every data-bearing kind, rolled back after
    probes = {
        "zz_recovery_probe_table": "unclassified table",
        "zz_recovery_probe_table_id_seq": "owned by unclassified",
        "zz_recovery_probe_parent": "unclassified table",
        "zz_recovery_probe_partition": "unclassified partition",
        "zz_recovery_probe_free_seq": "unclassified free-standing sequence",
        "zz_recovery_probe_domain_seq": "owned by restored table",
        "zz_recovery_probe_view": "of kind 'v'",
        "zz_recovery_probe_matview": "of kind 'm'",
        "zz_recovery_probe_type": "of kind 'c'",
    }
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE clinic_app.zz_recovery_probe_table (
                    id bigserial PRIMARY KEY
                );
                CREATE TABLE clinic_app.zz_recovery_probe_parent (
                    id integer NOT NULL
                ) PARTITION BY RANGE (id);
                CREATE TABLE clinic_app.zz_recovery_probe_partition
                    PARTITION OF clinic_app.zz_recovery_probe_parent
                    FOR VALUES FROM (0) TO (10);
                CREATE SEQUENCE clinic_app.zz_recovery_probe_free_seq;
                CREATE SEQUENCE clinic_app.zz_recovery_probe_domain_seq
                    OWNED BY clinic_app.identity_userpreference.theme;
                CREATE VIEW clinic_app.zz_recovery_probe_view AS SELECT 1 AS one;
                CREATE MATERIALIZED VIEW clinic_app.zz_recovery_probe_matview
                    AS SELECT 1 AS one;
                CREATE TYPE clinic_app.zz_recovery_probe_type AS (one integer);
                """
            )
        # When: the census and the rehearsal's runtime gate read the catalog
        violations = dict(unclassified(catalog_relations()))
        runtime = _runtime_unexpected_relations()
        transaction.set_rollback(True)

    # Then: exactly the probes fail, each by name and with its reason
    assert set(violations) == set(probes), violations
    for name, reason in probes.items():
        assert reason in violations[name], violations[name]
        assert "ops/testing/restore_contract.py" in violations[name]
    assert set(runtime) == {
        "zz_recovery_probe_parent",
        "zz_recovery_probe_partition",
        "zz_recovery_probe_table",
    }
    assert unclassified(catalog_relations()) == []


def test_partitioned_domain_parent_and_foreign_schema_fail() -> None:
    parent = Relation(APP_SCHEMA, "zz_parent", "p", partition=False)
    outside = Relation("public", "zz_outside", "r", partition=False)
    moved = Relation(
        APP_SCHEMA,
        "audit_event_seq_seq",
        "S",
        partition=False,
        owner_table="identity_user",
        owner_column="id",
    )
    manifest = Manifest(domain=(*restore_contract.DOMAIN_RELATIONS, "zz_parent"))

    violations = dict(unclassified((parent, outside, moved), manifest))

    assert set(violations) == {"zz_parent", "zz_outside", "audit_event_seq_seq"}
    assert "carries no TABLE DATA" in violations["zz_parent"]
    assert "outside clinic_app" in violations["zz_outside"]
    assert "catalog owner" in violations["audit_event_seq_seq"]

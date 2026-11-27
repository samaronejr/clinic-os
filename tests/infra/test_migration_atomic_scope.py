"""Fail closed on changes to the two admitted F6 atomic publications."""

from __future__ import annotations

import inspect
import json
from hashlib import sha256
from typing import TYPE_CHECKING

from apps.tenancy import fields, migration_operations
from apps.tenancy.migrations import _resumable_schema
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.writer import OperationWriter

if TYPE_CHECKING:
    from django.db.migrations.operations.base import Operation

LANE = {
    "ai.0001_initial",
    "ai.0002_agent_execution",
    "ai.0003_artifact_retention",
    "ai.0004_document_extraction",
    "ai.0005_evaluation",
    "billing.0013_fiscal_documents",
    "comms.0008_omnichannel_inbox",
    "ehr.0014_longitudinal_record",
    "ehr.0015_specialty_packs",
    "identity.0019_identity_lifecycle",
    "insurance.0001_revenue_cycle",
    "intake.0013_patient_demographics",
    "intake.0017_patient_aliases",
    "intake.0029_membership_payer",
    "interop.0001_import_framework",
    "inventory.0001_inventory",
    "orders.0001_identity_safe_results",
    "prescription.0012_clinical_document_kinds",
    "prescription.0013_class_catalog_directions",
    "prescription.0015_sncr_issuance",
    "scheduling.0011_insurer_membership",
    "scribe.0001_initial",
    "scribe.0003_transcripts",
    "scribe.0005_note_drafts",
    "workflows.0006_care_plans",
}
ATOMIC_PINS = {
    (
        "billing",
        "0013_fiscal_documents",
    ): "b39af2a25ad0ffdc0d0faecab253c842fc57c9c75bff22c80e8ad5558fcc48be",
    (
        "prescription",
        "0015_sncr_issuance",
    ): "747bd0f73043d7728ae6fd3d0eb8277f4531818a4360ad933545916c272103bf",
}


def _effective_operation(operation: Operation) -> list[object]:
    cls = type(operation)
    source = inspect.getsource(cls) if cls.__module__.startswith("apps.") else None
    rendered, imports = OperationWriter(operation).serialize()
    result: list[object] = [
        f"{cls.__module__}.{cls.__qualname__}",
        source,
        operation.atomic,
        operation.reversible,
        rendered,
        sorted(imports),
    ]
    if isinstance(operation, migrations.SeparateDatabaseAndState):
        database = operation.database_operations
        result.append([_effective_operation(op) for op in database])
        result.append([_effective_operation(op) for op in operation.state_operations])
    if isinstance(operation, migrations.RunSQL):
        # OperationWriter renders the constructor; also bind the active bytes.
        result.extend([operation.sql, operation.reverse_sql, operation.hints])
    return result


def operation_digest(migration: migrations.Migration) -> str:
    payload = [
        inspect.getsource(type(migration)),
        inspect.getsource(_resumable_schema),
        inspect.getsource(migration_operations),
        inspect.getsource(fields),
        migration.dependencies,
        migration.run_before,
        migration.replaces,
        [_effective_operation(operation) for operation in migration.operations],
    ]
    return sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def assert_atomic_admission(loader: MigrationLoader) -> None:
    known = {key for key in loader.disk_migrations if ".".join(key) in LANE}
    assert {".".join(key) for key in known} == LANE
    wrappers = (
        _resumable_schema.GuardedMigration,
        _resumable_schema.AtomicSchemaMigration,
    )
    lane = known | {
        key
        for key, migration in loader.disk_migrations.items()
        if isinstance(migration, wrappers)
    }
    atomic = {key for key in lane if loader.disk_migrations[key].atomic}
    assert atomic == ATOMIC_PINS.keys(), "unapproved atomic F6 migration set"
    actual = {
        key: operation_digest(loader.disk_migrations[key]) for key in sorted(atomic)
    }
    assert actual == ATOMIC_PINS, (
        "atomic F6 operations changed: rerun executed laws and review the pin"
    )


def test_atomic_lane_migrations_match_audited_operation_pins() -> None:
    assert_atomic_admission(MigrationLoader(None))

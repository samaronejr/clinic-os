"""Owner-loaded synthetic historical AI fixtures, not runtime approval authority.

The released base models supply defaults and envelope encryption. Only user
triggers are suspended while loading the fixture; CHECK/UNIQUE/FK constraints
remain active. Trigger suspension and rows share one atomic transaction, so a
failed load rolls both back. Every trigger is enabled before snapshot/upgrade.
No guard is disabled during migration or any forward assertion.
"""

from __future__ import annotations

import os
from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from django.apps import apps
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ModelState
from django.utils import timezone
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from typing import ClassVar

    from django.db.models import Manager, Model

    from ops.testing.migration_upgrade_wave_h_floor import Snapshot


class _DeclareRows(Protocol):
    def __call__(
        self, rows: LegacyRows, markers: Mapping[str, object], enrollment_id: object
    ) -> dict[str, object]: ...


class _HistoricalModel(Protocol):
    objects: ClassVar[Manager[Model]]


class _RetainedChecks(Protocol):
    def __call__(
        self, history: Mapping[str, str], *, upgraded: bool
    ) -> dict[str, bool]: ...


class LegacyRows:
    """Build one coherent old-schema graph without importing candidate models."""

    def __init__(self, organization: UUID, clinic: UUID) -> None:
        """Bind every loaded tenant row to the existing representative clinic."""
        self.organization = organization
        self.clinic = clinic
        self.tables: dict[str, list[str]] = {}

    def make(self, label: str, **fields: object) -> Model:
        """Load a historical row with constraints active and its trigger restored."""
        model = apps.get_model(label)
        state = ModelState.from_model(model)
        table = str(
            state.options.get("db_table", state.app_label + "_" + state.name.lower())
        )
        if table not in self.tables:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass "
                    "AND NOT tgisinternal AND tgenabled<>'O'",
                    ["clinic_app." + table],
                )
                if cursor.fetchone() != (0,):
                    message = "historical fixture requires enabled user triggers"
                    raise RuntimeError(message)
                cursor.execute(
                    sql.SQL("ALTER TABLE clinic_app.{} DISABLE TRIGGER USER").format(
                        sql.Identifier(table)
                    )
                )
                self.tables[table] = [
                    field.name
                    for field in connection.introspection.get_table_description(
                        cursor, table
                    )
                ]
        if "organization" in state.fields:
            fields.setdefault("organization_id", self.organization)
        if "clinic" in state.fields:
            fields.setdefault("clinic_id", self.clinic)
        manager = cast("type[_HistoricalModel]", model).objects
        # Analytics intentionally has no owner RLS policy. Keep its existing
        # writer grants and requester policies active even in the fixture loader.
        if table.startswith("analytics_"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SET LOCAL ROLE clinic_resolver"
                    if table == "analytics_metricdefinitionversion"
                    else "SET LOCAL ROLE clinic_app"
                )
                row = manager.create(**fields)
                cursor.execute("RESET ROLE")
                return row
        return manager.create(**fields)

    def find(self, label: str, **fields: object) -> Model:
        """Resolve another declared row using its natural key."""
        return cast("type[_HistoricalModel]", apps.get_model(label)).objects.get(
            **fields
        )

    @staticmethod
    def now() -> datetime:
        """Use the native fixture clock."""
        return timezone.now()

    @staticmethod
    def uuid() -> UUID:
        """Allocate generated fixture values through the normal UUID source."""
        return uuid4()

    def finish(self) -> dict[str, Snapshot]:
        """Restore and sample each table at Wave H's native pre-probe boundary."""
        capture_floor = run_path(
            str(Path(__file__).with_name("migration_upgrade_wave_h_floor.py"))
        )["capture_floor"]
        snapshots: dict[str, Snapshot] = {}
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, columns in self.tables.items():
                cursor.execute(
                    sql.SQL("ALTER TABLE clinic_app.{} ENABLE TRIGGER USER").format(
                        sql.Identifier(table)
                    )
                )
                snapshot = cast("dict[str, Snapshot]", capture_floor(cursor, (table,)))[
                    table
                ]
                if set(snapshot["columns"]) != set(columns):
                    message = f"Wave H floor columns changed during seed: {table}"
                    raise RuntimeError(message)
                snapshot["columns"] = columns
                snapshots[table] = snapshot
            # Native extraction links its deferred observation FK before inserting
            # that observation. Restore normal timing after validating the load.
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        return snapshots


def _baseline_approvals(approvals: Sequence[str], *, rollout_installed: bool) -> None:
    """Check each legacy approval against the activation guard on this base."""
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT count(*), bool_and(clinic_app.ai_eval_activation_allowed("
            "configuration_id,clinic_id) = %s) "
            "FROM clinic_app.ai_modelchangerequest WHERE id = ANY(%s::uuid[])",
            [not rollout_installed, list(approvals)],
        )
        if cursor.fetchone() != (len(approvals), True):
            message = (
                "historical approvals disagree with the installed activation guard"
            )
            raise RuntimeError(message)
        cursor.execute("RESET ROLE")


def _baseline_retained(
    retained: Mapping[str, str], *, retained_fence_installed: bool
) -> dict[str, bool]:
    helper = run_path(
        str(
            Path(os.environ["UPGRADE_HELPER_ROOT"])
            / "ops/testing/migration_upgrade_ai_retained.py"
        )
    )
    checks = cast("_RetainedChecks", helper["retained_checks"])(
        retained, upgraded=retained_fence_installed
    )
    if not all(checks.values()):
        message = (
            "historical retained paths disagree with the installed guards: "
            + ", ".join(name for name, passed in checks.items() if not passed)
        )
        raise RuntimeError(message)
    return checks


def seed_ai(markers: Mapping[str, object]) -> dict[str, object]:
    """Retain pre-rollout approvals, pending work, outputs and ASR provenance."""
    organization = UUID(str(markers["organization_id"]))
    owner = UUID(str(markers["owner_id"]))
    patient = UUID(str(markers["patient_id"]))
    fixtures = LegacyRows(organization, UUID(str(markers["clinic_id"])))
    with transaction.atomic(), connection.cursor() as cursor:
        # These guards landed independently. Read the base database's migration
        # ledger, not the candidate's models or the caller's revision name.
        applied = MigrationRecorder(connection).applied_migrations()
        rollout_installed = ("ai", "0014_staged_rollout") in applied
        retained_fence_installed = ("ai", "0016_retained_effects") in applied
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(organization), str(owner)],
        )
        enrollment = apps.get_model("intake.PatientClinicEnrollment")
        manager = cast("type[_HistoricalModel]", enrollment).objects
        enrollment_id = manager.get(patient_id=patient, clinic_id=fixtures.clinic).pk
        declaration = cast(
            "_DeclareRows",
            run_path(str(Path(__file__).with_name("migration_upgrade_ai_recipe.py")))[
                "declare_ai_rows"
            ],
        )(fixtures, markers, enrollment_id)
        # Capture once, before either probe and before leaving the original
        # atomic seed block. Probes neither receive nor refresh these digests.
        snapshots = fixtures.finish()
        _baseline_approvals(
            cast("list[str]", declaration["approvals"]),
            rollout_installed=rollout_installed,
        )
        checks = _baseline_retained(
            cast("dict[str, str]", declaration["retained"]),
            retained_fence_installed=retained_fence_installed,
        )
    return {
        **declaration,
        "tables": snapshots,
        "base_legacy_activation_allowed": not rollout_installed,
        "base_retained_fence_installed": retained_fence_installed,
        "base_retained_assertions": checks,
        "fixture_trigger_policy": (
            "owner insert-only suspension; all enabled before upgrade"
        ),
    }

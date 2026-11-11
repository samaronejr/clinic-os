"""Import rollback activity v2: a reference created after interop.0001 counts."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.enterprise_access import EmergencyScope, grant_break_glass
from apps.interop import services
from apps.interop.migrations import _import_activity_v2_sql
from apps.interop.models import ImportedRecord
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone

from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from interop.support import committed, import_world, operating, patients_csv

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

# Identity 0051 creates the grants and applies v2; unapplying it restores v1.
BEFORE_ENTERPRISE = ("identity", "0050_totp_secret_boundary")
BREAK_GLASS = (
    "EXISTS(SELECT 1 FROM clinic_app.identity_breakglassgrant x"
    " WHERE x.enrollment_id=c.enrollment_id)"
)


def _source() -> str:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT prosrc FROM pg_catalog.pg_proc"
            " WHERE oid='clinic_app.interop_hide_reversible(uuid)'::regprocedure"
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0])


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_rollback_retains_an_imported_patient_opened_by_break_glass(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    report, _receipt = committed(world, patients_csv(3), monkeypatch)
    with owner_context(rbac_graph.organization_a):
        opened = ImportedRecord.objects.get(
            job_id=report.job.job_id, row_number=1
        ).enrollment_id
    # A clinician outside any care relationship opens one imported chart.
    enterprise_setup(rbac_graph)
    clinician, _own = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(clinician)
    with permission_context(rbac_graph, clinician):
        grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(opened),
            reason="Emergencia sintetica",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(client),
        )
    with operating(world):
        reconciliation = services.reconciliation_report(
            clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
        )
        rolled = services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=reconciliation.job.sequence,
            reason="data_quality",
            session_key=world.session,
        )
    # The opened enrollment is in use: retained for review, the others hidden.
    assert rolled.job.state == "rolled_back"
    assert rolled.retained_enrollment_ids == (opened,)
    assert rolled.hidden == 2


def test_activity_v2_reverses_to_the_exact_v1_check() -> None:
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    v2 = _source()
    try:
        executor.migrate([BEFORE_ENTERPRISE])
        v1 = _source()
    finally:
        MigrationExecutor(connection).migrate(targets)
    assert _source() == v2
    assert v2 == _import_activity_v2_sql.FORWARD.split("$f$")[1]
    assert v1 == _import_activity_v2_sql.REVERSE.split("$f$")[1]
    assert BREAK_GLASS in v2
    assert BREAK_GLASS not in v1

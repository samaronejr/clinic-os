"""Every new relation is forced tenant-scoped, including source task linkage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.orders.models import ResultWorkItem
from apps.orders.services import acknowledge_result, reject_result, release_result
from django.apps import apps
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import laboratory
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_every_new_populated_relation_denies_cross_organization_reads(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    value = json.loads(lab.body())
    value["critical"] = True
    value["observations"] = [
        {
            "code": "cbc",
            "value": "SINTETICO-SENTINELA-ORDERS-31",
            "unit": "source",
            "reference_interval": "source",
            "abnormal_flag": "critical",
        }
    ]
    receipt = lab.receive(json.dumps(value).encode())
    wrong = json.loads(lab.body())
    wrong["identity"]["name"] = "Pessoa SINTETICA diferente"
    quarantined = lab.receive(json.dumps(wrong).encode())
    with as_actor(rbac_graph, rbac_graph.physician):
        acknowledge_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            disposition="urgent_review",
        )
        release_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=receipt.version_id,
            audience="patient",
        )
    with as_actor(rbac_graph, rbac_graph.shared_user):
        reject_result(
            clinic_id=rbac_graph.clinic_a,
            version_id=quarantined.version_id,
            evidence_code="wrong_patient_confirmed",
        )
    tables = sorted(
        model._meta.db_table for model in apps.get_app_config("orders").get_models()
    )
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        for table in tables:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM clinic_app.{}")
                .format(sql.Identifier(table))
                .as_string()
            )
            assert cursor.fetchone()[0] > 0, table
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='clinic_app'::regnamespace AND relname=ANY(%s)",
            [tables],
        )
        assert sorted(cursor.fetchall()) == [(table, True, True) for table in tables]
        cursor.execute("SELECT payload::text FROM clinic_app.audit_event")
        assert all(
            "SINTETICO-SENTINELA-ORDERS-31" not in payload
            for (payload,) in cursor.fetchall()
        )
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true),"
            "set_config('app.current_tenant',%s,true)",
            [str(rbac_graph.physician), str(rbac_graph.organization_b)],
        )
        for table in tables:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM clinic_app.{}")
                .format(sql.Identifier(table))
                .as_string()
            )
            assert cursor.fetchone() == (0,), table


def test_a_null_creator_cannot_forge_the_private_source_task_bridge(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
) -> None:
    lab = laboratory(rbac_graph, settings)
    receipt = lab.receive(lab.body())
    with owner_context(rbac_graph.organization_a):
        task = ResultWorkItem.objects.get(version_id=receipt.version_id).task
    task.pk, task.idempotency_key = uuid4(), uuid4()
    with as_actor(rbac_graph, rbac_graph.physician):
        with pytest.raises(DatabaseError) as denied, transaction.atomic():
            task.save(force_insert=True)
        assert getattr(denied.value.__cause__, "sqlstate", None) == "23514"

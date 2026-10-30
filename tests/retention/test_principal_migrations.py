"""The full migration chain preserves existing machine grant versions."""

from __future__ import annotations

from itertools import product
from uuid import UUID, uuid4

import pytest
from apps.identity.models import (
    Clinic,
    Organization,
    ServicePrincipal,
    ServicePrincipalGrant,
)
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from patients.test_intake_migrations import _default_connection, _scratch_database

pytestmark = pytest.mark.django_db(transaction=True)
ORGANIZATION = UUID(int=430039)
CLINIC = UUID(int=430040)
PAIRS = ((1, "appointment.read"), (2, "agent.execute"), (3, "retention.execute"))


def _principal(version: int) -> ServicePrincipal:
    return ServicePrincipal.objects.create(
        organization_id=ORGANIZATION,
        clinic_id=CLINIC,
        name=f"synthetic-migration-v{version}",
        purpose="retention" if version == 3 else "synthetic-migration",
        db_identity=f"clinic_agent_migration_v{version}",
        grant_set_version=version,
    )


def _grant(principal: ServicePrincipal, version: int, permission: str) -> None:
    ServicePrincipalGrant.objects.create(
        organization_id=ORGANIZATION,
        principal=principal,
        grant_set_version=version,
        permission=permission,
        subject_scope="clinic",
    )


def _authority_catalog() -> list[tuple[object, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname,pg_get_functiondef(p.oid),"
            "pg_get_userbyid(p.proowner),p.proacl::text,p.proconfig "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_app' AND p.proname IN "
            "('principal_scope','has_permission','identity_actor_permission',"
            "'identity_agent_grant_version','identity_principal_immutable') "
            "ORDER BY p.proname"
        )
        return [tuple(row) for row in cursor.fetchall()]


def _assert_closed_pairs() -> None:
    for (version, permission), wrong_permission in product(PAIRS, dict(PAIRS).values()):
        principal = ServicePrincipal.objects.get(grant_set_version=version)
        if permission != wrong_permission:
            with pytest.raises(IntegrityError), transaction.atomic():
                _grant(principal, version, wrong_permission)
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipalGrant.objects.create(
                organization_id=ORGANIZATION,
                principal=principal,
                grant_set_version=version,
                permission=permission,
                subject_scope="patient",
            )
    principal = ServicePrincipal.objects.get(grant_set_version=3)
    with pytest.raises(IntegrityError), transaction.atomic():
        _grant(principal, 1, "appointment.read")
    with pytest.raises(IntegrityError), transaction.atomic():
        ServicePrincipal.objects.filter(pk=principal.pk).update(grant_set_version=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        ServicePrincipalGrant.objects.filter(principal=principal).update(
            grant_set_version=2, permission="agent.execute"
        )


def test_full_chain_preserves_existing_v1_v2_and_admits_only_v3_retention(
    superuser_database_url: str,
) -> None:
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        MigrationExecutor(connection).migrate([("identity", "0039_ai_policy")])
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)", [str(ORGANIZATION)]
            )
            Organization.objects.create(
                id=ORGANIZATION,
                name="Synthetic Migration Organization",
                cnpj="00000000430039",
            )
            Clinic.objects.create(
                id=CLINIC,
                organization_id=ORGANIZATION,
                name="Synthetic Migration Clinic",
                crm_uf="SP",
                timezone="America/Sao_Paulo",
            )
            v1 = _principal(1)
            cursor.execute(
                "INSERT INTO clinic_app.identity_serviceprincipalgrant "
                "(id,organization_id,principal_id,permission,subject_scope,active) "
                "VALUES(%s,%s,%s,'appointment.read','clinic',true)",
                [uuid4(), ORGANIZATION, v1.pk],
            )
        MigrationExecutor(connection).migrate(
            [("identity", "0040_agent_execution_authority")]
        )
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)", [str(ORGANIZATION)]
            )
            _grant(_principal(2), 2, "agent.execute")
            before = _authority_catalog()
            assert set(
                ServicePrincipalGrant.objects.values_list(
                    "grant_set_version", "permission"
                )
            ) == set(PAIRS[:2])
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)", [str(ORGANIZATION)]
            )
            assert _authority_catalog() == before
            _grant(_principal(3), 3, "retention.execute")
            assert set(
                ServicePrincipalGrant.objects.values_list(
                    "grant_set_version", "permission"
                )
            ) == set(PAIRS)
            _assert_closed_pairs()
            cursor.execute(
                "SELECT tgname,tgenabled FROM pg_trigger "
                "WHERE tgrelid='clinic_app.identity_serviceprincipalgrant'::regclass "
                "AND NOT tgisinternal ORDER BY tgname"
            )
            assert {
                ("principal_grant_version", "O"),
                ("principal_immutable", "O"),
            } <= set(cursor.fetchall())
            cursor.execute(
                "SELECT to_regprocedure("
                "'clinic_app.retention_principal_scope(uuid,uuid)') IS NOT NULL"
            )
            assert cursor.fetchone() == (True,)
        applied = MigrationExecutor(connection).recorder.applied_migrations()
        assert {
            ("identity", "0040_agent_execution_authority"),
            ("identity", "0041_retention_authority"),
            ("ai", "0002_agent_execution"),
            ("ai", "0003_artifact_retention"),
            ("retention", "0005_transient_purge"),
        } <= applied.keys()

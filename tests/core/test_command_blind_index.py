"""Exact palette matching must select ciphertext by HMAC before revealing it."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

import pytest
from apps.intake.models import (
    DemographicsCorrection,
    Patient,
    PatientClinicEnrollment,
    PatientDemographics,
)
from apps.intake.patient_name_index import name_indexes
from apps.tenancy.envelope import issue_tenant_key
from django.apps import apps
from django.db import connection, models

from core.test_navigation import (
    OTHER,
    PATIENT,
    _api,
    _client_for,
    _get,
    _post,
)
from identity.permission_support import owner_context
from patient_http_support import seed_patients

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("endpoint", ["api", "options", "native"])
def test_exact_search_does_not_decrypt_nonmatches(
    rbac_graph: RbacGraph, endpoint: str
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    seed_patients(graph, user.pk, graph.clinic_a, (PATIENT, OTHER))
    _get(client, f"/scheduling/clinics/{graph.clinic_a}/agenda/")
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        digest = name_indexes(PATIENT)[-1]
        assert Patient.objects.filter(full_name_index=digest).count() == 1
        cursor.execute(
            "SELECT id, full_name_index, birth_date "
            "FROM clinic_app.intake_patient WHERE full_name_index <> %s",
            [digest],
        )
        other_id, other_index, other_birth = cursor.fetchone()
        enrollment = PatientClinicEnrollment.objects.get(patient_id=other_id)
        latest = PatientDemographics.objects.filter(patient_id=other_id).aggregate(
            latest=models.Max("version")
        )["latest"]
        corrupt = b"invalid-envelope"
        version = PatientDemographics.objects.create(
            organization_id=graph.organization_a,
            patient_id=other_id,
            clinic_id=enrollment.clinic_id,
            enrollment_id=enrollment.pk,
            version=(latest or 0) + 1,
            registry_full_name=corrupt,
            registry_full_name_index=bytes(other_index)
            if other_index is not None
            else None,
            registry_birth_date=bytes(other_birth) if other_birth is not None else None,
        )
        DemographicsCorrection.objects.create(
            organization_id=graph.organization_a,
            clinic_id=enrollment.clinic_id,
            patient_id=other_id,
            demographics=version,
            actor_id=user.pk,
            actor_label="corrupt-fixture",
            changed_fields=[],
        )
        # A decrypt-and-filter scan cannot succeed: the other envelope is corrupt.
        cursor.execute(
            "UPDATE clinic_app.intake_patient SET full_name = %s "
            "WHERE full_name_index <> %s",
            [corrupt, digest],
        )
    for query, match in [(PATIENT.lower(), True), ("Sintetico Navegacao", False)]:
        if endpoint == "api":
            response = _api(client, {"q": query, "clinic_id": str(graph.clinic_a)})
        else:
            path = (
                "/workspace/command/options/"
                if endpoint == "options"
                else "/workspace/command/"
            )
            response = _post(client, path, {"q": query})
        assert response.status_code == 200
        assert (PATIENT.encode() in response.content) is match
        assert OTHER.encode() not in response.content


def test_exact_name_indexes_survive_key_rotation(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    seed_patients(graph, user.pk, graph.clinic_a, (PATIENT,))
    with owner_context(graph.organization_a):
        before = name_indexes(PATIENT)
        stored = Patient.objects.get().full_name_index
        assert stored is not None
        assert bytes(stored) == before[-1]
        issue_tenant_key()
        after = name_indexes(PATIENT)
        assert after[:-1] == before
        assert after[-1] != before[-1]
    response = _api(client, {"q": PATIENT, "clinic_id": str(graph.clinic_a)})
    assert response.status_code == 200
    assert [row["label"] for row in response.json() if row["kind"] == "patient"] == [
        PATIENT
    ]


def test_ambiguous_exact_matches_return_a_disabled_notice(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    seed_patients(graph, user.pk, graph.clinic_a, (PATIENT,) * 6)
    response = _api(client, {"q": PATIENT, "clinic_id": str(graph.clinic_a)})
    assert response.status_code == 200
    rows = response.json()
    assert [row["kind"] for row in rows] == ["notice"]
    assert 'aria-disabled="true"' in rows[0]["html"]
    assert rows[0]["token"] is None
    assert PATIENT not in response.content.decode()


def test_existing_encrypted_patients_are_backfilled(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    client, user = _client_for(graph, "receptionist")
    seed_patients(graph, user.pk, graph.clinic_a, (PATIENT,))
    migration = import_module("apps.intake.migrations.0012_patient_name_index")
    with owner_context(graph.organization_a):
        patient = Patient.objects.get()
        enrollment = PatientClinicEnrollment.objects.get(patient_id=patient.pk)
    with connection.schema_editor() as editor:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
            cursor.execute(
                "SELECT id, full_name, birth_date "
                "FROM clinic_app.intake_patient WHERE id = %s",
                [str(patient.pk)],
            )
            patient_id, name_bytes, birth_bytes = cursor.fetchone()
            latest = PatientDemographics.objects.filter(
                patient_id=patient_id
            ).aggregate(latest=models.Max("version"))["latest"]
            version = PatientDemographics.objects.create(
                organization_id=graph.organization_a,
                patient_id=patient_id,
                clinic_id=enrollment.clinic_id,
                enrollment_id=enrollment.pk,
                version=(latest or 0) + 1,
                registry_full_name=bytes(name_bytes),
                registry_full_name_index=None,
                registry_birth_date=bytes(birth_bytes)
                if birth_bytes is not None
                else None,
            )
            DemographicsCorrection.objects.create(
                organization_id=graph.organization_a,
                clinic_id=enrollment.clinic_id,
                patient_id=patient_id,
                demographics=version,
                actor_id=user.pk,
                actor_label="backfill-fixture",
                changed_fields=[],
            )
            Patient.objects.filter(pk=patient_id).update(full_name_index=None)
            # The backfill UPDATE re-fires the mirror guard: record the digest
            # version it restores, in the same transaction.
            restored = PatientDemographics.objects.create(
                organization_id=graph.organization_a,
                patient_id=patient_id,
                clinic_id=enrollment.clinic_id,
                enrollment_id=enrollment.pk,
                version=(latest or 0) + 2,
                registry_full_name=bytes(name_bytes),
                registry_full_name_index=name_indexes(PATIENT)[-1],
                registry_birth_date=bytes(birth_bytes)
                if birth_bytes is not None
                else None,
            )
            DemographicsCorrection.objects.create(
                organization_id=graph.organization_a,
                clinic_id=enrollment.clinic_id,
                patient_id=patient_id,
                demographics=restored,
                previous=version,
                actor_id=user.pk,
                actor_label="backfill-fixture",
                changed_fields=[],
            )
        migration.backfill(apps, editor)
    response = _api(client, {"q": PATIENT, "clinic_id": str(graph.clinic_a)})
    assert response.status_code == 200
    assert [row["kind"] for row in response.json()] == ["patient"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'clinic_app.intake_patient'::regclass"
        )
        assert cursor.fetchone() == (True, True)


def test_blind_index_function_has_closed_database_authority() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proowner::regrole::text, prosecdef, proconfig FROM pg_proc "
            "WHERE oid = "
            "'clinic_app.protected_blind_index(text,text,bytea)'::regprocedure"
        )
        assert cursor.fetchone() == (
            "clinic_owner",
            True,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
        )
        cursor.execute(
            "SELECT a.grantee::regrole::text, a.privilege_type FROM pg_proc p "
            "CROSS JOIN LATERAL aclexplode(p.proacl) a WHERE p.oid = "
            "'clinic_app.protected_blind_index(text,text,bytea)'::regprocedure"
        )
        assert set(cursor.fetchall()) == {
            ("clinic_owner", "EXECUTE"),
            ("clinic_app", "EXECUTE"),
        }


def test_blind_index_is_tenant_bound(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    with owner_context(graph.organization_a):
        first = name_indexes(PATIENT)
    with owner_context(graph.organization_b):
        other = name_indexes(PATIENT)
    assert not set(first) & set(other)

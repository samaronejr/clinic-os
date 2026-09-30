"""Exact palette matching must select ciphertext by HMAC before revealing it."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

import pytest
from apps.intake.models import Patient
from apps.intake.patient_name_index import name_indexes
from apps.tenancy.envelope import issue_tenant_key
from django.apps import apps
from django.db import connection

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
        # A decrypt-and-filter scan cannot succeed: the other envelope is corrupt.
        cursor.execute(
            "UPDATE clinic_app.intake_patient SET full_name = %s "
            "WHERE full_name_index <> %s",
            [b"invalid-envelope", digest],
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
    with owner_context(graph.organization_a):
        Patient.objects.update(full_name_index=None)
    migration = import_module("apps.intake.migrations.0012_patient_name_index")
    with connection.schema_editor() as editor:
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

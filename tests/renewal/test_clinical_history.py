"""Longitudinal acceptance against real PostgreSQL and clinic_app authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TYPE_CHECKING, TypedDict, Unpack
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.history import HistoryChange, read_history, save_history
from apps.ehr.models import Allergy, Encounter, HistoryAssessment, Problem
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    open_encounter,
)
from apps.identity.models import UserClinicRole
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction

from patient_service_support import runtime_role
from renewal.test_encounters import physician_client, seed, setup_context

if TYPE_CHECKING:
    from uuid import UUID

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def begin(graph: RbacGraph) -> Encounter:
    appointment, _ = seed(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)


class HistoryOverrides(TypedDict, total=False):
    kind: str
    expected_revision: int
    state: str
    description: str
    status: str
    reason: str
    entry_id: UUID | None


def save(
    encounter: Encounter, **changes: Unpack[HistoryOverrides]
) -> HistoryAssessment:
    change = HistoryChange(
        kind="problem",
        expected_revision=0,
        description="Problema sintético",
        status="active",
        reason="Registro inicial",
        state="documented",
    )
    return save_history(
        clinic_id=encounter.clinic_id,
        encounter_id=encounter.pk,
        change=replace(change, **changes),
    )


@pytest.mark.parametrize(
    ("kind", "model"), [("problem", Problem), ("allergy", Allergy)]
)
def test_record_correct_resolve_and_explicit_assessment(
    rbac_graph: RbacGraph, kind: str, model: type[Problem | Allergy]
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        context = read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind=kind
        )
        assert context.state == "not_assessed"
        assert context.revision == 0
        assessment = save(
            encounter, kind=kind, state="none_documented", description="", status=""
        )
        stored = HistoryAssessment.objects.get(pk=assessment.pk)
        assert assessment.author_label == stored.author_label
        assert assessment.author_label
        assert assessment.created_at == stored.created_at
        context = read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind=kind
        )
        assert context.state == "none_documented"
        assert not context.entries
        save(encounter, kind=kind, expected_revision=1)
        first = model.objects.get()
        save(
            encounter,
            kind=kind,
            entry_id=first.entry_id,
            expected_revision=2,
            description="Valor corrigido",
            reason="Correção sintética",
        )
        save(
            encounter,
            kind=kind,
            entry_id=first.entry_id,
            expected_revision=3,
            description="Valor corrigido",
            status="resolved",
            reason="Resolução sintética",
        )
        context = read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind=kind
        )
        assert context.state == "documented"
        assert context.revision == 4
        assert len(context.entries) == 1
        assert context.entries[0].status == "resolved"
        assert list(
            model.objects.order_by("version").values_list("description", "status")
        ) == [
            ("Problema sintético", "active"),
            ("Valor corrigido", "active"),
            ("Valor corrigido", "resolved"),
        ]
        assert all(
            row.author_id == graph.physician and row.created_at and row.author_label
            for row in context.assessments
        )
        with pytest.raises(ClinicalConflictError, match="documented_entries"):
            save(
                encounter,
                kind=kind,
                expected_revision=4,
                state="none_documented",
                description="",
                status="",
            )
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            save(encounter, kind=kind, expected_revision=2, entry_id=first.entry_id)
        assert model.objects.count() == 3
    with setup_context(graph.organization_a):
        events = AuditEvent.objects.filter(event_type__startswith="ehr.history.")
        assert events.filter(event_type="ehr.history.saved").count() == 4
        assert all(set(e.payload) <= {"clinic_id", "object_verb"} for e in events)


@pytest.mark.parametrize(
    "changes",
    [
        {"description": "  "},
        {"state": ""},
        {"state": "none_documented", "description": "", "status": "", "reason": " "},
        {"state": "none_documented", "description": "not blank"},
        {"status": "unknown"},
    ],
)
def test_blank_never_becomes_none(
    rbac_graph: RbacGraph, changes: HistoryOverrides
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(ValidationError):
            save(encounter, **changes)
        assert not HistoryAssessment.objects.exists()


@pytest.mark.parametrize("actor", ["shared_user", "clinic_admin"])
def test_deny_role_other_clinic_and_raw_history(
    rbac_graph: RbacGraph, actor: str
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        save(encounter)
    with runtime_role(), tenant_context(getattr(graph, actor), graph.organization_a):
        for model in (Problem, Allergy, HistoryAssessment):
            assert not model.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            read_history(
                clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind="problem"
            )
        with pytest.raises(ClinicalAccessDeniedError):
            save(encounter, expected_revision=1)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        read_history(
            clinic_id=graph.clinic_b, encounter_id=encounter.pk, kind="problem"
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not HistoryAssessment.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            save(encounter)


def test_immutable_raw_versions_and_race(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    barrier = Barrier(2, timeout=15)

    def compete(_: int) -> str:
        try:
            with runtime_role(), tenant_context(graph.physician, graph.organization_a):
                barrier.wait()
                try:
                    save(encounter)
                except ClinicalConflictError as error:
                    return error.reason_code
                return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, range(2))) == ["saved", "stale_revision"]
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert Problem.objects.count() == HistoryAssessment.objects.count() == 1
        for model in (Problem, HistoryAssessment):
            with pytest.raises(DatabaseError), transaction.atomic():
                model.objects.all().delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            Problem.objects.update(description="overwrite")
    with (
        setup_context(graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Problem.objects.update(description="owner overwrite")


def test_care_reader_cannot_amend_other_encounter_and_revoked_role(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        save(encounter)
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="physician",
        )
    with (
        runtime_role(),
        tenant_context(graph.clinic_admin, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind="problem"
        )
    # Reassignment must retain the scheduling contract's covering availability.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.clinic_admin,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
    # A historical appointment provides the contract's explicit care relationship.
    with setup_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.scheduling_appointment "
            "SET practitioner_id=%s WHERE id=%s",
            [graph.clinic_admin, encounter.appointment_id],
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        context = read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind="problem"
        )
        assert context.entries[0].description == "Problema sintético"
        assert not context.can_write
        with pytest.raises(ClinicalAccessDeniedError):
            save(encounter, expected_revision=1)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # Authorship remains a care relationship after assignment changes.
        assert read_history(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, kind="problem"
        ).entries
    with setup_context(graph.organization_a):
        # Keep tenant membership so this checks clinical RLS after role revocation.
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.physician,
            role="receptionist",
        )
        UserClinicRole.objects.filter(
            user_id=graph.physician, role="physician"
        ).delete()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not Problem.objects.exists()


def test_http_save_stale_invalid_and_body_binding(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    url = f"/ehr/clinics/{graph.clinic_a}/history/"
    with physician_client(graph) as client:
        assert (
            client.post(
                url, {"action": "open", "encounter_id": encounter.pk}
            ).status_code
            == 302
        )
        page = client.get(url)
        assert page.status_code == 200
        assert "no-store" in page.headers["Cache-Control"]
        body = {
            "action": "save",
            "encounter_id": encounter.pk,
            "kind": "problem",
            "revision": 0,
            "state": "documented",
            "description": "Valor HTTP",
            "status": "active",
            "reason": "Registro",
        }
        assert client.post(url, body | {"description": " "}).status_code == 400
        assert client.post(url, body).status_code == 302
        stale = client.post(url, body | {"description": "Não salvo"})
        assert stale.status_code == 409
        assert stale.context["form"].data["description"] == "Não salvo"
        assert client.post(url, body | {"encounter_id": uuid4()}).status_code == 403
        assert (
            client.get(url).context["sections"][0].entries[0].description
            == "Valor HTTP"
        )


def test_exact_history_rls_and_grants() -> None:
    tables = ["ehr_historyassessment", "ehr_problem", "ehr_allergy"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,relowner::regrole::text "
            "FROM pg_class WHERE relname=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (t, True, True, "clinic_owner") for t in tables
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (t, p)
            for t in tables
            for p in ("setup_tenant", "history_read", "history_insert")
        }
        cursor.execute(
            "SELECT table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (t, p) for t in tables for p in ("SELECT", "INSERT")
        }

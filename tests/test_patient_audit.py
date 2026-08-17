from __future__ import annotations

import inspect
import logging
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.intake import patient_creation, patient_search
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class ForcedAuditFailureError(Exception):
    pass


def test_audit_failure_after_domain_inserts_rolls_back_everything(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_key = uuid4()
    failed_key = uuid4()
    original_append = record_phase1_event
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        patient_creation.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=first_key,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT seq, curr_hash FROM clinic_app.audit_event_tenant ORDER BY seq"
            )
            chain_before = cursor.fetchall()

        def append_then_fail(
            event_type: str,
            *,
            clinic_id: UUID,
            affected_record_id: UUID,
        ) -> int:
            original_append(
                event_type,
                clinic_id=clinic_id,
                affected_record_id=affected_record_id,
            )
            raise ForcedAuditFailureError

        monkeypatch.setattr(
            "apps.intake.patient_creation.record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedAuditFailureError):
            patient_creation.create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name="Bea Synthetic",
                birth_date=date(2001, 2, 3),
                idempotency_key=failed_key,
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
            patient_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM clinic_app.intake_patientclinicenrollment"
            )
            enrollment_count = cursor.fetchone()
            cursor.execute(
                "SELECT seq, curr_hash FROM clinic_app.audit_event_tenant ORDER BY seq"
            )
            chain_after = cursor.fetchall()

        monkeypatch.setattr(
            "apps.intake.patient_creation.record_phase1_event",
            original_append,
        )
        retried = patient_creation.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Bea Synthetic",
            birth_date=date(2001, 2, 3),
            idempotency_key=failed_key,
        )
        verification = verify_chain(rbac_graph.organization_a)

    assert patient_count == (1,)
    assert enrollment_count == (1,)
    assert chain_after == chain_before
    assert retried.enrollment.idempotency_key == failed_key
    assert verification.valid is True
    assert verification.row_count == 2


def test_accepted_search_emits_one_metadata_event_without_url_or_log_leakage(
    rbac_graph: RbacGraph,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    query = "Synthetic Needle"
    registration = None
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = patient_creation.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic Needle",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )
        result = patient_search.search_patients(
            clinic_id=rbac_graph.clinic_a,
            query=query,
            page=1,
            birth_date=date(2000, 1, 2),
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb', payload::text "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'intake.patient.searched' ORDER BY seq"
            )
            search_events = cursor.fetchall()

    assert registration is not None
    assert len(result.items) == 1
    assert search_events == [
        (
            "intake.patient.searched",
            "identity.clinic",
            str(rbac_graph.clinic_a),
            2,
            str(rbac_graph.clinic_a),
            "searched",
            '{"clinic_id": "'
            + str(rbac_graph.clinic_a)
            + '", "object_verb": "searched"}',
        )
    ]
    search_event_text = repr(search_events)
    assert query not in search_event_text
    assert "2000-01-02" not in search_event_text
    assert str(registration.patient.pk) not in search_event_text
    assert str(registration.enrollment.pk) not in search_event_text
    assert all(query not in record.getMessage() for record in caplog.records)
    assert list(inspect.signature(patient_search.search_patients).parameters) == [
        "clinic_id",
        "query",
        "page",
        "birth_date",
    ]


def test_failed_search_audit_append_rolls_back_the_search_event(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = record_phase1_event

    def append_then_fail(
        event_type: str,
        *,
        clinic_id: UUID,
        affected_record_id: UUID,
    ) -> int:
        original_append(
            event_type,
            clinic_id=clinic_id,
            affected_record_id=affected_record_id,
        )
        raise ForcedAuditFailureError

    monkeypatch.setattr(
        "apps.intake.patient_search.record_phase1_event",
        append_then_fail,
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        with pytest.raises(ForcedAuditFailureError):
            patient_search.search_patients(
                clinic_id=rbac_graph.clinic_a,
                query="Missing Synthetic",
                page=1,
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert audit_count == (0,)

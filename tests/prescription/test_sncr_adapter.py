"""Todo 36 issuance commands never promote a synthetic artifact to issued."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.models import UserClinicRole
from apps.prescription import issuance
from apps.prescription.class_values import PrescriptionClass
from apps.prescription.models import (
    PrescriptionDocument,
    PrescriptionDraft,
    PrescriptionIssuance,
    SignatureOperation,
    SncrNumberAllocation,
)
from apps.prescription.services import (
    issue_prescription,
    render_document,
    save_draft,
)
from apps.prescription.sncr import (
    CLASS_MODELS,
    AllocationRequest,
    RecordedSncrAdapter,
    SncrAdapter,
    SncrUnavailableError,
)
from apps.prescription.verification import revoke_document
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.test import override_settings
from psycopg.errors import CheckViolation

from patient_service_support import runtime_role
from provider_gate_support import activate_capability, seed_capabilities
from renewal.test_document_artifacts import ITEM
from renewal.test_signatures import NOW, SigningSetup, owner_scope
from renewal.test_signatures import setup as signing_setup

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ("signing_setup",)
pytestmark = pytest.mark.django_db(transaction=True)

FIXTURE = Path(__file__).parent / "fixtures" / "sncr-documentation.json"
REQUEST = AllocationRequest(
    category="notificacao_a",
    council="CRM",
    jurisdiction="SP",
    registration="999999999999",
    quantity=25,
)


@pytest.fixture(autouse=True)
def rehearsal() -> Iterator[None]:
    with override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=True):
        yield


def recording(*, retained: bool = False) -> bytes:
    data = json.loads(FIXTURE.read_text())
    return json.dumps(data["retained" if retained else "notification"]).encode()


def counts(setup: SigningSetup) -> tuple[int, ...]:
    with owner_scope(setup.graph.organization_a):
        return tuple(
            model.objects.count()
            for model in (
                AuditEvent,
                IntegrationOperation,
                SignatureOperation,
                PrescriptionIssuance,
                SncrNumberAllocation,
            )
        )


def classified(setup: SigningSetup, category: str | None) -> PrescriptionDocument:
    with (
        runtime_role(),
        tenant_context(setup.graph.physician, setup.graph.organization_a),
    ):
        draft = save_draft(
            **setup.scope,
            draft_id=setup.document.draft_id,
            category=category,
            expected_version=setup.document.document_version,
            items=[ITEM],
        )
        return render_document(
            **setup.scope, draft_id=draft.pk, expected_version=draft.version
        )


def test_fixture_issuance_is_idempotent_rehearsal(signing_setup: SigningSetup) -> None:
    signature = signing_setup.issue()
    graph = signing_setup.graph
    command = uuid4()
    with (
        override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=True),
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        first = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=command,
        )
        second = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=command,
        )
        assert first.pk == second.pk
        assert first.state == "rehearsal_complete"
        assert first.signature_id == signature.pk
        assert not SignatureOperation.objects.filter(state="issued").exists()
        assert PrescriptionIssuance.objects.count() == 1
    with owner_scope(graph.organization_a):
        events = AuditEvent.objects.filter(event_type="prescription.issuance.rehearsed")
        assert events.count() == 1
        assert set(events.get().payload) <= {
            "clinic_id",
            "object_verb",
            "http_method",
            "http_status",
            "reason_code",
            "request_id",
        }


@pytest.mark.parametrize("category", list(CLASS_MODELS))
@pytest.mark.parametrize("seeded", [False, True])
def test_regulated_class_without_activated_sncr_is_side_effect_free(
    signing_setup: SigningSetup, category: str, seeded: bool
) -> None:
    if seeded:
        seed_capabilities()
    document = classified(signing_setup, category)
    before = counts(signing_setup)
    graph = signing_setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(SncrUnavailableError, match=r"^sncr_required$"):
            issue_prescription(
                clinic_id=graph.clinic_a, document_id=document.pk, command_id=uuid4()
            )
        # The caller catches the refusal inside its tenant transaction.
        assert not PrescriptionIssuance.objects.exists()
    assert counts(signing_setup) == before


def test_activated_sncr_is_not_live_authority(signing_setup: SigningSetup) -> None:
    seed_capabilities()
    activate_capability("sncr")
    document = classified(signing_setup, "controle_especial")
    before = counts(signing_setup)
    graph = signing_setup.graph
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(SncrUnavailableError, match=r"^class_not_enabled$"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a, document_id=document.pk, command_id=uuid4()
        )
    assert counts(signing_setup) == before


@pytest.mark.parametrize("category", [None, "simple"])
def test_real_simple_or_unselected_is_not_issuable(
    signing_setup: SigningSetup, category: str | None
) -> None:
    document = classified(signing_setup, category)
    before = counts(signing_setup)
    graph = signing_setup.graph
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises((SncrUnavailableError, ValidationError)),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a, document_id=document.pk, command_id=uuid4()
        )
    assert counts(signing_setup) == before


def test_feature_off_refuses_without_creating_an_operation(
    signing_setup: SigningSetup,
) -> None:
    graph = signing_setup.graph
    before = counts(signing_setup)
    with (
        override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=False),
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(SncrUnavailableError),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before


@pytest.mark.parametrize("selector", ["missing", "wrong_clinic", "other_actor"])
def test_issuance_authority_refusals_have_no_audit_or_outbox_effect(
    signing_setup: SigningSetup, selector: str
) -> None:
    graph = signing_setup.graph
    before = counts(signing_setup)
    clinic = graph.clinic_b if selector == "wrong_clinic" else graph.clinic_a
    document = uuid4() if selector == "missing" else signing_setup.document.pk
    actor = graph.shared_user if selector == "other_actor" else graph.physician
    with (
        runtime_role(),
        tenant_context(actor, graph.organization_a),
        pytest.raises(ClinicalAccessDeniedError),
    ):
        issue_prescription(clinic_id=clinic, document_id=document, command_id=uuid4())
    assert counts(signing_setup) == before


@pytest.mark.parametrize("command", [UUID(int=0), "not-a-uuid", None])
def test_command_boundary_rejects_invalid_id(
    signing_setup: SigningSetup, command: UUID
) -> None:
    graph = signing_setup.graph
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ValidationError),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=command,
        )
    assert counts(signing_setup) == before


def test_unsigned_or_stale_document_refuses(signing_setup: SigningSetup) -> None:
    graph = signing_setup.graph
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalConflictError, match="verified_signature_required"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before
    signing_setup.issue()
    classified(signing_setup, "synthetic_non_controlled")
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalConflictError, match="stale_document_approval"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before


def test_revoked_document_refuses(signing_setup: SigningSetup) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        revoke_document(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            reason="issuer_request",
        )
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalConflictError, match="document_revoked"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before


def test_duplicate_command_concurrently_creates_one_receipt(
    signing_setup: SigningSetup,
) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    command = uuid4()
    ready = Barrier(2, timeout=20)

    def invoke() -> UUID:
        connections.close_all()
        try:
            with runtime_role(), tenant_context(graph.physician, graph.organization_a):
                ready.wait()
                return issue_prescription(
                    clinic_id=graph.clinic_a,
                    document_id=signing_setup.document.pk,
                    command_id=command,
                ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert results[0] == results[1]
    with owner_scope(graph.organization_a):
        assert PrescriptionIssuance.objects.count() == 1
        assert (
            AuditEvent.objects.filter(
                event_type="prescription.issuance.rehearsed"
            ).count()
            == 1
        )


@pytest.mark.parametrize("category", list(CLASS_MODELS))
def test_documented_models_bind_exact_request_and_prescriber(category: str) -> None:
    retained = CLASS_MODELS[category] in {"RCE", "RET"}
    request = replace(
        REQUEST,
        category=category,
        quantity=1 if retained else 25,
        cnpj="11111111111111" if retained else "",
    )
    adapter: SncrAdapter = RecordedSncrAdapter(
        request=request,
        response=recording(retained=retained),
        status=201 if retained else 200,
    )
    result = adapter.allocate(request)
    assert result.request == request
    assert result.quantity == (1000 if retained else 2)
    assert result.response == recording(retained=retained)
    assert request.payload()["documento"] == REQUEST.registration
    assert request.endpoint.endswith(
        "receita-especial-retencao" if retained else "notificacao-receita"
    )
    with pytest.raises(SncrUnavailableError, match="class_not_enabled"):
        adapter.register_emission(document_digest="a" * 64)
    assert RecordedSncrAdapter.real_enabled is False


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (b"", 400, "sncr_numbering_unavailable"),
        (b"", 404, "sncr_invalid_recording"),
        (b"", 422, "sncr_invalid_recording"),
        (None, 200, "sncr_reconcile"),
        (b"{}", 500, "sncr_invalid_recording"),
        (b"[]", 200, "sncr_invalid_recording"),
        (b"{", 200, "sncr_invalid_recording"),
        (b"{}", 200, "sncr_invalid_recording"),
        (b'{"a":1,"a":2}', 200, "sncr_invalid_recording"),
        (b"x" * 16385, 200, "sncr_invalid_recording"),
    ],
)
def test_recorded_exhaustion_timeout_and_invalid_responses(
    signing_setup: SigningSetup, response: bytes | None, status: int, code: str
) -> None:
    before = counts(signing_setup)
    adapter = RecordedSncrAdapter(request=REQUEST, response=response, status=status)
    with pytest.raises(SncrUnavailableError, match=f"^{code}$"):
        adapter.allocate(REQUEST)
    assert counts(signing_setup) == before
    with owner_scope(signing_setup.graph.organization_a):
        assert (
            PrescriptionDraft.objects.get(pk=signing_setup.document.draft_id).state
            == "draft"
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"documentoSolicitante": "CRM-SP-123"},
        {"dataSolicitacao": "invalid"},
        {"dataSolicitacao": None},
        {"numeracoesReceita": "manufactured"},
        {"numeracoesReceita": None},
        {"quantidade": True},
        {"quantidade": 2},
        {"ativo": False},
        {"saldoReceitas": True},
        {"saldoReceitas": -1},
        {"saldoReceitas": 2147483648},
        {"saldoReceitas": "49"},
        {"mensagem": None},
        {"numeracoesReceita": "[]"},
        {"numeracoesReceita": "[2411.1-00.0000001, 2411.1-00.0000001]"},
        {"unexpected": "SINTETICO-SENTINELA-SNCR"},
    ],
)
def test_mismatched_or_unknown_recording_fields_refuse(
    changed: dict[str, object],
) -> None:
    facts = json.loads(recording())
    facts.update(changed)
    adapter = RecordedSncrAdapter(request=REQUEST, response=json.dumps(facts).encode())
    with pytest.raises(SncrUnavailableError, match="sncr_invalid_recording"):
        adapter.allocate(REQUEST)


def test_prescriber_class_jurisdiction_and_payload_cannot_be_substituted() -> None:
    adapter = RecordedSncrAdapter(request=REQUEST, response=recording())
    for changes in (
        {"registration": "123"},
        {"category": "notificacao_b"},
        {"jurisdiction": "RJ"},
        {"council": "CRO"},
        {"quantity": 2},
        {"category": "simple"},
        {"jurisdiction": "XX"},
        {"registration": "SINTETICO-SENTINELA-SNCR"},
        {"council": "invalid"},
        {"quantity": 0},
        {"cnpj": "123"},
    ):
        altered = replace(REQUEST, **changes)
        with pytest.raises(SncrUnavailableError, match="sncr_invalid_recording"):
            adapter.allocate(altered)


def test_recorded_adapter_is_explicitly_disabled_without_rehearsal() -> None:
    adapter = RecordedSncrAdapter(request=REQUEST, response=recording())
    with (
        override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=False),
        pytest.raises(SncrUnavailableError, match="class_not_enabled"),
    ):
        adapter.allocate(REQUEST)
    with pytest.raises(SncrUnavailableError, match="sncr_invalid_recording"):
        adapter.register_emission(document_digest="invalid")


@pytest.mark.parametrize(
    ("first", "last"),
    [
        ("", "2602.6-53.0001000"),
        ("2602.6-53.0000001", "2602.6-53.0001001"),
        ("2602.6-53.0000001", "2602.6-54.0001000"),
        ("2602.6-53.0001000", "2602.6-53.0000001"),
        ("11000000000001", "11000000001000"),
    ],
)
def test_undocumented_or_extended_retained_block_refuses(first: str, last: str) -> None:
    request = replace(
        REQUEST, category="controle_especial", quantity=1, cnpj="11111111111111"
    )
    facts = json.loads(recording(retained=True))
    facts.update(inicio=first, fim=last)
    with pytest.raises(SncrUnavailableError, match="sncr_invalid_recording"):
        RecordedSncrAdapter(
            request=request, response=json.dumps(facts).encode(), status=201
        ).allocate(request)


def test_retained_payload_does_not_accept_ignored_quantity_or_invalid_cnpj() -> None:
    request = replace(
        REQUEST, category="controle_especial", quantity=1, cnpj="11111111111111"
    )
    assert replace(request, cnpj="99999999999999").payload()["cnpj"] == "99999999999999"
    for altered in (
        replace(request, quantity=2),
        replace(request, cnpj="invalid"),
        replace(request, cnpj=""),
    ):
        with pytest.raises(SncrUnavailableError, match="sncr_invalid_recording"):
            altered.payload()


def test_invalid_mode_refuses_adapter_and_issuance(
    signing_setup: SigningSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apps.prescription.sncr.resolved_data_mode", lambda: None)
    monkeypatch.setattr(issuance, "resolved_data_mode", lambda: None)
    with pytest.raises(SncrUnavailableError):
        RecordedSncrAdapter(request=REQUEST, response=recording()).allocate(REQUEST)
    graph = signing_setup.graph
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(SncrUnavailableError),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before


def test_new_command_cannot_reissue_a_completed_document(
    signing_setup: SigningSetup,
) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        first = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalConflictError, match="already_completed"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=first.document_id,
            command_id=uuid4(),
        )
    assert counts(signing_setup) == before


def test_same_command_cannot_be_rebound_to_another_document(
    signing_setup: SigningSetup,
) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    command = uuid4()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        first = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=command,
        )
    newer = classified(signing_setup, "synthetic_non_controlled")
    assert newer.pk != first.document_id
    before = counts(signing_setup)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(ClinicalConflictError, match="issuance_command_conflict"),
    ):
        issue_prescription(
            clinic_id=graph.clinic_a, document_id=newer.pk, command_id=command
        )
    assert counts(signing_setup) == before


def test_receipt_is_immutable_and_hidden_from_other_actors(
    signing_setup: SigningSetup,
) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receipt = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
    for actor, organization in (
        (graph.shared_user, graph.organization_a),
        (graph.shared_user, graph.organization_b),
    ):
        with runtime_role(), tenant_context(actor, organization):
            assert not PrescriptionIssuance.objects.exists()
            assert not SncrNumberAllocation.objects.exists()
    with owner_scope(graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionIssuance.objects.filter(pk=receipt.pk).update(state="issued")
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM clinic_app.prescription_prescriptionissuance WHERE id=%s",
                [receipt.pk],
            )
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE clinic_app.prescription_prescriptionissuance")


def test_owner_recording_is_encrypted_immutable_and_not_runtime_writable(
    signing_setup: SigningSetup,
) -> None:
    graph = signing_setup.graph
    facts = {
        "synthetic": True,
        "request": REQUEST.payload(),
        "response": json.loads(recording()),
        "source": "documentation-only",
    }
    with owner_scope(graph.organization_a):
        allocation = SncrNumberAllocation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            issuer_id=graph.physician,
            category=PrescriptionClass.NOTIFICACAO_A,
            request_digest=sha256(recording()).hexdigest(),
            recording=facts,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT recording FROM clinic_app.prescription_sncrnumberallocation "
                "WHERE id=%s",
                [allocation.pk],
            )
            ciphertext = bytes(cursor.fetchone()[0])
            assert REQUEST.registration.encode() not in ciphertext
            assert b"2411.1-00.0000001" not in ciphertext
        with pytest.raises(DatabaseError), transaction.atomic():
            SncrNumberAllocation.objects.filter(pk=allocation.pk).update(
                category="notificacao_b"
            )
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE clinic_app.prescription_sncrnumberallocation")
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert SncrNumberAllocation.objects.get(pk=allocation.pk).recording == facts
        with pytest.raises(DatabaseError), transaction.atomic():
            SncrNumberAllocation.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                issuer_id=graph.physician,
                category="notificacao_a",
                request_digest="b" * 64,
                recording=facts,
            )


def test_cross_clinic_revocation_hides_existing_issuance_and_allocation(
    signing_setup: SigningSetup,
) -> None:
    signing_setup.issue()
    graph = signing_setup.graph
    with owner_scope(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        allocation = SncrNumberAllocation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            issuer_id=graph.physician,
            category=PrescriptionClass.NOTIFICACAO_A,
            request_digest=sha256(recording()).hexdigest(),
            recording={"synthetic": True, "response": json.loads(recording())},
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receipt = issue_prescription(
            clinic_id=graph.clinic_a,
            document_id=signing_setup.document.pk,
            command_id=uuid4(),
        )
        assert SncrNumberAllocation.objects.filter(pk=allocation.pk).exists()
        assert PrescriptionIssuance.objects.filter(pk=receipt.pk).exists()
    with owner_scope(graph.organization_a):
        UserClinicRole.objects.filter(
            clinic_id=graph.clinic_a,
            user_id=graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        ).update(revoked_at=NOW)
    before = counts(signing_setup)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert UserClinicRole.objects.filter(
            clinic_id=graph.clinic_b, role=UserClinicRole.Role.PHYSICIAN
        ).exists()
        assert not PrescriptionIssuance.objects.filter(pk=receipt.pk).exists()
        assert not SncrNumberAllocation.objects.filter(pk=allocation.pk).exists()
        with pytest.raises(ClinicalAccessDeniedError):
            issue_prescription(
                clinic_id=graph.clinic_b,
                document_id=signing_setup.document.pk,
                command_id=uuid4(),
            )
    assert counts(signing_setup) == before


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_issuance_trigger_binds_the_stored_physician_role(
    signing_setup: SigningSetup, role: str
) -> None:
    signature = signing_setup.issue()
    graph = signing_setup.graph
    with owner_scope(graph.organization_a):
        creations = (
            lambda: SncrNumberAllocation.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                issuer_id=graph.physician,
                category=PrescriptionClass.NOTIFICACAO_A,
                request_digest=sha256(recording()).hexdigest(),
                recording={"synthetic": True, "response": json.loads(recording())},
            ),
            lambda: PrescriptionIssuance.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                issuer_id=graph.physician,
                document=signing_setup.document,
                signature=signature,
                command_id=uuid4(),
            ),
        )
        if role == "physician":
            # Valid artifacts isolate the role decision from every other guard.
            with transaction.atomic():
                for create in creations:
                    assert create().issuer_id == graph.physician
                transaction.set_rollback(True)
        UserClinicRole.objects.filter(
            user_id=graph.physician,
            clinic_id=graph.clinic_a,
            role=UserClinicRole.Role.PHYSICIAN,
        ).update(revoked_at=NOW)
        if role != "physician":
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=graph.physician,
                role=role,
            )
        # Owner writes bypass runtime RLS, so this must be the real trigger's
        # stored-role decision, including a physician whose role was revoked.
        for create in creations:
            with pytest.raises(DatabaseError) as refusal, transaction.atomic():
                create()
            cause = refusal.value.__cause__
            assert isinstance(cause, CheckViolation)
            context = cause.diag.context
            assert context is not None
            assert "prescription_issuance_guard" in context


def test_allocation_and_receipt_exact_posture() -> None:
    tables = [
        "prescription_prescriptionissuance",
        "prescription_sncrnumberallocation",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relnamespace='clinic_app'::regnamespace AND relname=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in tables}
        cursor.execute(
            "SELECT tablename,policyname,cmd,roles::text FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            (tables[0], "setup_tenant", "ALL", "{clinic_owner}"),
            (tables[0], "issuer_read", "SELECT", "{clinic_app}"),
            (tables[0], "issuer_insert", "INSERT", "{clinic_app}"),
            (tables[1], "setup_tenant", "ALL", "{clinic_owner}"),
            (tables[1], "issuer_read", "SELECT", "{clinic_app}"),
        }
        cursor.execute(
            "SELECT grantee,table_name,privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' AND grantee IN ('clinic_app','PUBLIC') "
            "AND table_name=ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            ("clinic_app", tables[0], "SELECT"),
            ("clinic_app", tables[0], "INSERT"),
            ("clinic_app", tables[1], "SELECT"),
        }

"""Prescription acceptance against real clinic_app PostgreSQL RLS and transactions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.finalization import close_encounter
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    open_encounter,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.prescription import services
from apps.prescription.forms import PrescriptionItemForm
from apps.prescription.models import PrescriptionDraft, PrescriptionItem
from apps.prescription.policy import ITEM_LIMITS, SYNTHETIC_CATEGORY, validate_category
from apps.prescription.services import (
    create_draft,
    discard_draft,
    draft_items,
    save_draft,
    view_draft,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client, setup_context

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ITEM = {
    "medication_description": " Medicamento fictício — não utilizar ",
    "strength_form": "Concentração e forma sintéticas",
    "dose": " Dose digitada pelo médico  ",
    "route": "Via sintética",
    "frequency": "Frequência sintética",
    "duration": "Duração sintética",
    "quantity": "Quantidade sintética",
    "instructions": "Texto explícito; sem cálculo automático.",
}


class Scope(TypedDict):
    clinic_id: UUID
    encounter_id: UUID
    patient_id: UUID
    issuer_id: UUID


def seed(graph: RbacGraph) -> Scope:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = create_synthetic_appointment(setup)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
    return Scope(
        clinic_id=graph.clinic_a,
        encounter_id=encounter.pk,
        patient_id=encounter.patient_id,
        issuer_id=graph.physician,
    )


def test_author_resume_exact_text_retained_versions_and_audit(
    rbac_graph: RbacGraph,
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        saved = save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        assert saved.version == 2
        assert draft_items(saved) == [ITEM]
        assert create_draft(**scope, category=SYNTHETIC_CATEGORY).pk == draft.pk
        resumed = view_draft(**scope, draft_id=draft.pk)
        assert resumed.encounter_id == scope["encounter_id"]
        assert resumed.patient_id == scope["patient_id"]
        assert resumed.issuer_id == scope["issuer_id"]
        changed = {**ITEM, "dose": "Nova dose explícita"}
        saved = save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=2,
            items=[changed, ITEM],
        )
        assert saved.version == 3
        assert draft_items(saved) == [changed, ITEM]
        assert PrescriptionItem.objects.get(draft=draft, version=2).dose == ITEM["dose"]
    with setup_context(rbac_graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type__startswith="prescription."
            ).values_list("event_type", "payload")
        )
        assert {event for event, _ in events} == {
            "prescription.draft.created",
            "prescription.draft.saved",
            "prescription.draft.viewed",
        }
        assert all(
            set(payload) == {"clinic_id", "object_verb"} for _, payload in events
        )
        assert ITEM["dose"] not in str(events)


@pytest.mark.parametrize(
    "category",
    [
        "controlled",
        "notification",
        "non_controlled",
        "antimicrobial",
        "unknown",
        "",
        "synthetic_non_controlled ",
    ],
)
def test_unconfirmed_categories_rejected_at_service(
    rbac_graph: RbacGraph, category: str
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with pytest.raises(ValidationError):
            create_draft(**scope, category=category)
        assert not PrescriptionDraft.objects.exists()
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        with pytest.raises(ValidationError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=category,
                expected_version=2,
                items=[{**ITEM, "dose": "Do not persist"}],
            )
        current = view_draft(**scope, draft_id=draft.pk)
        assert current.version == 2
        assert draft_items(current) == [ITEM]


@pytest.mark.parametrize(
    "field", ["patient_id", "issuer_id", "encounter_id", "clinic_id"]
)
def test_wrong_scope_never_rebinds(rbac_graph: RbacGraph, field: str) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        forged = {
            "clinic_id": scope["clinic_id"],
            "encounter_id": scope["encounter_id"],
            "patient_id": scope["patient_id"],
            "issuer_id": scope["issuer_id"],
        }
        forged[field] = uuid4()
        with pytest.raises((ClinicalAccessDeniedError, CurrentActorError)):
            save_draft(
                clinic_id=forged["clinic_id"],
                encounter_id=forged["encounter_id"],
                patient_id=forged["patient_id"],
                issuer_id=forged["issuer_id"],
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=1,
                items=[ITEM],
            )
        assert view_draft(**scope, draft_id=draft.pk).version == 1
        assert not PrescriptionItem.objects.exists()


@pytest.mark.parametrize("actor", ["shared_user", "clinic_admin", "no_membership"])
def test_other_roles_cannot_read_or_write(rbac_graph: RbacGraph, actor: str) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
    actor_id = (
        User.objects.get(username=rbac_graph.no_membership_username).pk
        if actor == "no_membership"
        else getattr(rbac_graph, actor)
    )
    if actor == "no_membership":
        with (
            pytest.raises(TenantAccessDeniedError),
            runtime_role(),
            tenant_context(actor_id, rbac_graph.organization_a),
        ):
            view_draft(**scope, draft_id=draft.pk)
        return
    with runtime_role(), tenant_context(actor_id, rbac_graph.organization_a):
        assert not PrescriptionDraft.objects.exists()
        assert not PrescriptionItem.objects.exists()
        with pytest.raises((ClinicalAccessDeniedError, CurrentActorError)):
            view_draft(**scope, draft_id=draft.pk)


def test_stale_and_failed_save_are_atomic(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=1,
                items=[{**ITEM, "dose": "stale"}],
            )
        original = services._event

        def fail_saved(draft: PrescriptionDraft, action: str) -> None:
            if action == "saved":
                message = "synthetic audit failure"
                raise DatabaseError(message)
            original(draft, action)

        monkeypatch.setattr(services, "_event", fail_saved)
        with pytest.raises(DatabaseError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=2,
                items=[{**ITEM, "dose": "rolled back"}],
            )
        current = view_draft(**scope, draft_id=draft.pk)
        assert current.version == 2
        assert draft_items(current) == [ITEM]
        assert PrescriptionItem.objects.count() == 1


@pytest.mark.parametrize("field", list(ITEM_LIMITS))
def test_bounded_fields_and_form_preserve_text(
    rbac_graph: RbacGraph, field: str
) -> None:
    scope = seed(rbac_graph)
    assert PrescriptionItemForm(ITEM).is_valid()
    form = PrescriptionItemForm(ITEM)
    assert form.is_valid()
    assert form.cleaned_data == ITEM
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        with pytest.raises(ValidationError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=1,
                items=[{**ITEM, field: "x" * (ITEM_LIMITS[field] + 1)}],
            )
        assert view_draft(**scope, draft_id=draft.pk).version == 1


def test_database_binding_rls_and_terminal_discard(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        with pytest.raises(ClinicalConflictError, match="draft_in_progress"):
            close_encounter(
                clinic_id=scope["clinic_id"], encounter_id=scope["encounter_id"]
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDraft.objects.filter(pk=draft.pk).update(version=9)
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDraft.objects.filter(pk=draft.pk).update(patient_id=uuid4())
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionItem.objects.filter(draft=draft).update(dose="forged")
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionItem.objects.filter(draft=draft).delete()
        discard_draft(**scope, draft_id=draft.pk, expected_version=2)
        assert not PrescriptionItem.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            view_draft(**scope, draft_id=draft.pk)
        assert (
            close_encounter(
                clinic_id=scope["clinic_id"], encounter_id=scope["encounter_id"]
            ).state
            == "closed"
        )
    with setup_context(rbac_graph.organization_a):
        assert PrescriptionItem.objects.count() == 1
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('prescription_prescriptiondraft',"
                "'prescription_prescriptionitem')"
            )
            assert cursor.fetchall() == [(True, True), (True, True)]
            cursor.execute(
                "SELECT tablename,policyname FROM pg_policies "
                "WHERE schemaname='clinic_app' AND tablename LIKE 'prescription_%'"
            )
            assert set(cursor.fetchall()) == {
                ("prescription_prescriptiondraft", policy)
                for policy in (
                    "setup_tenant",
                    "draft_read",
                    "draft_insert",
                    "draft_update",
                )
            } | {
                ("prescription_prescriptionitem", policy)
                for policy in ("setup_tenant", "item_read", "item_insert")
            } | {
                ("prescription_prescriptiondocument", policy)
                for policy in ("setup_tenant", "document_read", "document_insert")
            } | {
                ("prescription_signatureoperation", policy)
                for policy in (
                    "setup_tenant",
                    "signature_read",
                    "signature_insert",
                    "signature_update",
                )
            } | {
                ("prescription_signaturecallback", policy)
                for policy in (
                    "setup_tenant",
                    "signature_callback_read",
                    "signature_callback_insert",
                )
            } | {
                ("prescription_prescriptiondocumentrelease", policy)
                for policy in (
                    "setup_tenant",
                    "release_read",
                    "release_insert",
                    "release_revoke",
                )
            } | {
                ("prescription_prescriptiondocumentrevocation", policy)
                for policy in (
                    "setup_tenant",
                    "revocation_read",
                    "revocation_insert",
                )
            }
            cursor.execute(
                "SELECT table_name,privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
                "AND table_name LIKE 'prescription_%'"
            )
            assert set(cursor.fetchall()) == {
                (table, privilege)
                for table in (
                    "prescription_prescriptiondraft",
                    "prescription_prescriptionitem",
                    "prescription_prescriptiondocument",
                    "prescription_signatureoperation",
                    "prescription_signaturecallback",
                    "prescription_prescriptiondocumentrevocation",
                    "prescription_prescriptiondocumentrelease",
                )
                for privilege in ("SELECT", "INSERT")
            }
            cursor.execute(
                "SELECT table_name,column_name "
                "FROM information_schema.role_column_grants "
                "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
                "AND table_name LIKE 'prescription_%' AND privilege_type='UPDATE'"
            )
            assert set(cursor.fetchall()) == {
                ("prescription_prescriptiondraft", column)
                for column in ("version", "state", "updated_at")
            } | {
                ("prescription_signatureoperation", column)
                for column in (
                    "state",
                    "operation_id",
                    "evidence_id",
                    "evidence_snapshot",
                    "authorized_until",
                    "signed_bytes",
                    "signed_digest",
                    "failure_reason",
                    "completed_at",
                )
            } | {
                ("prescription_prescriptiondocumentrelease", column)
                for column in ("revoked_by_id", "revoked_at")
            }


def test_closed_encounter_rejects_new_draft(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        close_encounter(
            clinic_id=scope["clinic_id"], encounter_id=scope["encounter_id"]
        )
        with pytest.raises(ClinicalConflictError, match="encounter_closed"):
            create_draft(**scope, category=SYNTHETIC_CATEGORY)


def test_concurrent_saves_have_one_winner(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
    barrier = Barrier(2, timeout=15)

    def save(index: int) -> str:
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.physician, rbac_graph.organization_a),
            ):
                barrier.wait()
                try:
                    save_draft(
                        **scope,
                        draft_id=draft.pk,
                        category=SYNTHETIC_CATEGORY,
                        expected_version=1,
                        items=[{**ITEM, "dose": str(index)}],
                    )
                except ClinicalConflictError as error:
                    return error.reason_code
                return "saved"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, [1, 2])) == ["saved", "stale_revision"]
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert PrescriptionItem.objects.count() == 1
        assert view_draft(**scope, draft_id=draft.pk).version == 2


def test_category_never_implies_real_issuance() -> None:
    assert validate_category(SYNTHETIC_CATEGORY) == "synthetic-draft-v1"
    with pytest.raises(NotImplementedError):
        services.issue_prescription()


def payload(scope: Scope, draft: PrescriptionDraft) -> dict[str, str]:
    return {
        "action": "save",
        "encounter_id": str(scope["encounter_id"]),
        "patient_id": str(scope["patient_id"]),
        "issuer_id": str(scope["issuer_id"]),
        "draft_id": str(draft.pk),
        "category": SYNTHETIC_CATEGORY,
        "expected_version": "1",
        "items-TOTAL_FORMS": "1",
        "items-INITIAL_FORMS": "0",
        "items-MIN_NUM_FORMS": "1",
        "items-MAX_NUM_FORMS": "20",
        **{f"items-0-{key}": value for key, value in ITEM.items()},
    }


def test_http_author_resume_errors_and_discard(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        assert client.get(url).status_code == 200
        assert client.post(url, {"action": "save"}).status_code == 403
        assert (
            client.post(
                url, {"action": "open", "encounter_id": scope["encounter_id"]}
            ).status_code
            == 302
        )
        assert client.get(url).context["draft"] is None
        creation = {"action": "create", **scope, "category": "controlled"}
        assert client.post(url, creation).status_code == 400
        assert (
            client.post(url, {**creation, "category": SYNTHETIC_CATEGORY}).status_code
            == 302
        )
        draft = client.get(url).context["draft"]
        data = payload(scope, draft)
        assert client.post(url, {**data, "action": "invalid"}).status_code == 403
        invalid = client.post(url, {**data, "items-0-dose": "x" * 161})
        assert invalid.status_code == 400
        assert invalid.context["items"].data["items-0-dose"] == "x" * 161
        assert client.post(url, {**data, "items-0-dose": "   "}).status_code == 400
        assert client.post(url, data).status_code == 302
        page = client.get(url)
        assert page.context["draft"].version == 2
        assert "no-store" in page.headers["Cache-Control"]
        assert page.context["items"].initial == [ITEM]
        conflict = client.post(url, {**data, "items-0-dose": "unsaved"})
        assert conflict.status_code == 409
        assert conflict.context["items"].data["items-0-dose"] == "unsaved"
        data = {**data, "items-INITIAL_FORMS": "1", "expected_version": "2"}
        for field in ("patient_id", "issuer_id", "draft_id", "encounter_id"):
            assert client.post(url, {**data, field: uuid4()}).status_code == 403
        assert (
            client.post(
                url, {**data, "action": "discard", "expected_version": "2"}
            ).status_code
            == 302
        )
        assert client.get(url).context["draft"].state == "discarded"
        assert client.get(url).context["items"] is None
        assert client.post(url, data).status_code == 403
        assert (
            client.post(url, {**creation, "category": SYNTHETIC_CATEGORY}).status_code
            == 400
        )


def test_http_real_database_failure_retains_unsaved_content(
    rbac_graph: RbacGraph,
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    # Owner installs a task-local rejected write; the request still uses
    # clinic_app. The dose column is a bytea envelope, so the rejection is
    # unconditional rather than a plaintext comparison.
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE clinic_app.prescription_prescriptionitem "
            "ADD CONSTRAINT task32_failure CHECK (false) NOT VALID"
        )
    try:
        with physician_client(rbac_graph) as client:
            client.post(url, {"action": "open", "encounter_id": scope["encounter_id"]})
            data = {**payload(scope, draft), "items-0-dose": "Unsaved retry"}
            failed = client.post(url, data)
            assert failed.status_code == 503
            assert failed.context["unsaved"] is True
            assert failed.context["items"].data["items-0-dose"] == "Unsaved retry"
            page = client.get(url)
            assert page.context["draft"].version == 1
            assert page.context["items"].initial == []
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE clinic_app.prescription_prescriptionitem "
                "DROP CONSTRAINT task32_failure"
            )


def test_peer_physician_and_cross_tenant_are_denied(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    peer = User.objects.create(username=f"synthetic-peer-{uuid4().hex}")
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=peer,
            role="physician",
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
    with runtime_role(), tenant_context(peer.pk, rbac_graph.organization_a):
        assert not PrescriptionDraft.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            view_draft(**scope, draft_id=draft.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            create_draft(**scope, category=SYNTHETIC_CATEGORY)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not PrescriptionDraft.objects.exists()
        assert not PrescriptionItem.objects.exists()
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=rbac_graph.physician, role="physician"
        ).delete()
    with (
        pytest.raises(TenantAccessDeniedError),
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        view_draft(**scope, draft_id=draft.pk)


@pytest.mark.parametrize(
    "invalid",
    [[], [ITEM] * 21, [{**ITEM, "unknown": "x"}], [{**ITEM, "dose": "\u0000"}]],
)
def test_item_set_validation_never_changes_draft(
    rbac_graph: RbacGraph, invalid: list[dict[str, str]]
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        with pytest.raises(ValidationError):
            save_draft(
                **scope,
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=1,
                items=invalid,
            )
        assert view_draft(**scope, draft_id=draft.pk).version == 1


def test_database_rejects_forged_creation_and_category(rbac_graph: RbacGraph) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        for field, value in (
            ("patient_id", uuid4()),
            ("issuer_id", uuid4()),
            ("category", "controlled"),
        ):
            fields = {
                **scope,
                "organization_id": rbac_graph.organization_a,
                "category": SYNTHETIC_CATEGORY,
                "contract_version": "synthetic-draft-v1",
            }
            fields[field] = value
            with pytest.raises(DatabaseError), transaction.atomic():
                PrescriptionDraft.objects.create(**fields)
        assert not PrescriptionDraft.objects.exists()

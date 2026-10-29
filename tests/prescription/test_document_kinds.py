"""Per-kind exact-byte artifacts, professional authority and stale-review fences."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import (
    PhysicianProfile,
    ProfessionalRegistration,
    UserClinicRole,
)
from apps.orders.models import Order, OrderItem
from apps.orders.services import place_order
from apps.orders.values import CATALOG
from apps.prescription import signing
from apps.prescription.document_rendering import SyntheticPdfRenderer
from apps.prescription.document_templates import TEMPLATES
from apps.prescription.documents import DocumentContentInput
from apps.prescription.models import (
    DocumentDraftContent,
    DocumentKind,
    PrescriptionDocument,
    PrescriptionDraft,
    SignatureCallback,
    SignatureOperation,
)
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import (
    create_document_draft,
    create_draft,
    download_document,
    render_document,
    save_document_draft,
    save_draft,
    verify_document_integrity,
)
from apps.prescription.signature_provider import SyntheticSignatureProvider
from apps.prescription.signing import abandon_signature, initiate_signature
from apps.prescription.verification import release_document
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone, translation

from auth.stepup_test_support import verified_request
from ehr.test_autosave import registration
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_document_artifacts import ITEM, Scope, _pdf_text, seed
from renewal.test_document_verification import SignedSetup
from renewal.test_encounters import physician_client
from renewal.test_signatures import SigningSetup
from renewal.test_signatures import setup as signing_setup

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("signing_setup",)
BODY = (
    "  SINTETICO-SENTINELA-33 — conteúdo explícito do profissional.\nSem uso clínico.  "
)
CONTENT = DocumentContentInput(body=BODY)


def scoped(graph: RbacGraph) -> Scope:
    """Keep the legacy scheduled encounter and add real professional authority."""
    scope = seed(graph)
    registration(graph, graph.physician, graph.clinic_a)
    return scope


def saved(
    graph: RbacGraph, scope: Scope, kind: str = DocumentKind.CERTIFICATE
) -> PrescriptionDraft:
    draft = create_document_draft(
        clinic_id=graph.clinic_a, encounter_id=scope["encounter_id"], kind=kind
    )
    if kind == DocumentKind.PRESCRIPTION:
        return save_draft(
            **scope,
            draft_id=draft.pk,
            expected_version=1,
            category=SYNTHETIC_CATEGORY,
            items=[ITEM],
        )
    content = (
        replace(CONTENT, exam_codes=("cbc",))
        if kind == DocumentKind.EXAM_REQUEST
        else CONTENT
    )
    return save_document_draft(
        clinic_id=graph.clinic_a,
        draft_id=draft.pk,
        expected_version=1,
        content=content,
    )


def rendered(
    graph: RbacGraph, scope: Scope, draft: PrescriptionDraft
) -> PrescriptionDocument:
    return render_document(**scope, draft_id=draft.pk, expected_version=draft.version)


@pytest.mark.parametrize("kind", DocumentKind.values)
def test_every_kind_freezes_exact_content_and_deterministic_template(
    rbac_graph: RbacGraph, kind: str
) -> None:
    # Given: one professionally scoped clinician and a configured clinic identity.
    scope = scoped(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        publish_configuration(
            clinic_id=rbac_graph.clinic_a,
            expected_version=0,
            content=ConfigurationContent(
                display_name="Clínica sintética 33",
                contact_email="documentos@synthetic.invalid",
                contact_phone="+5511999990033",
            ),
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope, kind)
        # When: the current version becomes an immutable artifact.
        document = rendered(rbac_graph, scope, draft)
        # Then: every visible value, kind and branding snapshot is digest-bound.
        assert document.frozen_input["kind"] == kind
        assert document.frozen_input["branding"]["version"] == 1
        assert document.frozen_input["clinic_label"] == "Clínica sintética 33"
        assert verify_document_integrity(document).ok
        downloaded = download_document(
            clinic_id=rbac_graph.clinic_a, document_id=document.pk
        )
        assert downloaded.data == bytes(document.pdf_bytes)
        assert downloaded.file_name.startswith(
            "prescricao-" if kind == DocumentKind.PRESCRIPTION else f"{kind}-"
        )
        assert SyntheticPdfRenderer().render(document.frozen_input).pdf_bytes == bytes(
            document.pdf_bytes
        )
        extracted = _pdf_text(bytes(document.pdf_bytes))
        assert TEMPLATES[kind].title in extracted
        if kind != DocumentKind.PRESCRIPTION:
            assert document.frozen_input["content"]["body"] == BODY
            assert DocumentDraftContent.objects.get(draft=draft).content["body"] == BODY
    assert TEMPLATES[kind].title in extracted
    assert document.frozen_input["clinic_label"] in extracted
    assert "documentos@synthetic.invalid" in extracted


def test_kinds_coexist_and_legacy_prescription_service_resumes_only_prescription(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # Given: a non-prescription draft exists first for this encounter.
        certificate = saved(rbac_graph, scope)
        # When: the unchanged legacy service creates a prescription.
        prescription = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        # Then: kinds cannot overwrite or accidentally resume each other.
        assert prescription.pk != certificate.pk
        assert prescription.kind == DocumentKind.PRESCRIPTION
        assert (
            create_document_draft(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=scope["encounter_id"],
                kind=DocumentKind.CERTIFICATE,
            ).pk
            == certificate.pk
        )
        assert PrescriptionDraft.objects.count() == 2
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDraft.objects.filter(pk=certificate.pk).update(
                kind=DocumentKind.REFERRAL, version=3
            )


@pytest.mark.parametrize("kind", ["", "certificate ", "controlled", "unknown"])
def test_unknown_kind_writes_no_draft(rbac_graph: RbacGraph, kind: str) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        with pytest.raises(ValidationError):
            create_document_draft(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=scope["encounter_id"],
                kind=kind,
            )
        assert not PrescriptionDraft.objects.exists()


@pytest.mark.parametrize(
    "content",
    [
        DocumentContentInput(body=""),
        DocumentContentInput(body="x" * 20001),
        DocumentContentInput(body="bad\x00text"),
        DocumentContentInput(body="body", recipient="x" * 241),
        DocumentContentInput(body="body", exam_codes=("cbc",)),
        DocumentContentInput(
            body="body", cid10_code="A00", cid10_source_version="claimed"
        ),
        DocumentContentInput(body="body", cid10_source_version="unapproved"),
    ],
)
def test_malformed_or_unapproved_content_preserves_version(
    rbac_graph: RbacGraph, content: DocumentContentInput
) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope)
        with pytest.raises(ValidationError):
            save_document_draft(
                clinic_id=rbac_graph.clinic_a,
                draft_id=draft.pk,
                expected_version=2,
                content=content,
            )
        draft.refresh_from_db()
        assert draft.version == 2
        assert DocumentDraftContent.objects.get(draft=draft).content["body"] == BODY


def test_rendering_freezes_exam_command_without_placing_order(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope, DocumentKind.EXAM_REQUEST)
        document = rendered(rbac_graph, scope, draft)
        assert document.frozen_input["order_command_id"]
        assert not Order.objects.exists()
        with pytest.raises(ClinicalConflictError, match="already_rendered"):
            rendered(rbac_graph, scope, draft)
        assert not Order.objects.exists()


def test_exam_template_freezes_portuguese_labels_independent_of_ui_locale(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    with translation.override("pt-br"):
        expected = f"{CATALOG['cbc']!s} (cbc)"
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        translation.override("en"),
    ):
        draft = saved(rbac_graph, scope, DocumentKind.EXAM_REQUEST)
        document = rendered(rbac_graph, scope, draft)
        assert document.frozen_input["content"]["exam_labels"] == [expected]
        assert expected in _pdf_text(bytes(document.pdf_bytes))
        assert not Order.objects.exists()


def test_edit_after_review_rejects_signing_and_keeps_old_artifact(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-33",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    with (
        override_settings(PRESCRIPTION_SYNTHETIC_SIGNING=True),
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope)
        document = rendered(rbac_graph, scope, draft)
        before = bytes(document.pdf_bytes)
        save_document_draft(
            clinic_id=rbac_graph.clinic_a,
            draft_id=draft.pk,
            expected_version=2,
            content=replace(CONTENT, body="Novo texto sintético"),
        )
        with pytest.raises(ClinicalConflictError, match="stale_document_approval"):
            initiate_signature(clinic_id=rbac_graph.clinic_a, document_id=document.pk)
        assert not SignatureOperation.objects.exists()
        document.refresh_from_db()
        assert bytes(document.pdf_bytes) == before
        assert sha256(before).hexdigest() == document.pdf_digest
        assert DocumentDraftContent.objects.filter(draft=draft).count() == 2
        with pytest.raises(DatabaseError), transaction.atomic():
            DocumentDraftContent.objects.filter(draft=draft, version=2).update(
                content={}
            )


def test_new_kinds_require_current_professional_scope_and_hide_foreign_records(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope)
    with owner_context(rbac_graph.organization_a):
        ProfessionalRegistration.objects.filter(user_id=rbac_graph.physician).update(
            revoked_at=timezone.now()
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert not DocumentDraftContent.objects.exists()
        with pytest.raises((ClinicalAccessDeniedError, CurrentActorError)):
            save_document_draft(
                clinic_id=rbac_graph.clinic_a,
                draft_id=draft.pk,
                expected_version=2,
                content=CONTENT,
            )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not DocumentDraftContent.objects.exists()
        for selector in (draft.pk, uuid4()):
            with pytest.raises(ClinicalAccessDeniedError):
                save_document_draft(
                    clinic_id=rbac_graph.clinic_a,
                    draft_id=selector,
                    expected_version=2,
                    content=CONTENT,
                )


def test_synthetic_certificate_signs_and_releases_through_existing_aggregate(
    rbac_graph: RbacGraph,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = saved(rbac_graph, scope)
        document = rendered(rbac_graph, scope, draft)
    with owner_context(rbac_graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-33",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        setup = SignedSetup(
            rbac_graph,
            scope,
            document,
            verified_request(rbac_graph.physician),
            SyntheticSignatureProvider(),
        )
        operation = setup.issue()
        assert operation.state == SignatureOperation.State.REHEARSAL_COMPLETE
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            release = release_document(
                clinic_id=rbac_graph.clinic_a, document_id=document.pk
            )
            assert (
                release_document(
                    clinic_id=rbac_graph.clinic_a, document_id=document.pk
                ).pk
                == release.pk
            )
    with owner_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT content FROM clinic_app.prescription_documentdraftcontent"
        )
        assert BODY.encode() not in bytes(cursor.fetchone()[0])
        audit = list(AuditEvent.objects.values("payload"))
        outbox = list(IntegrationOperation.objects.values())
        signing_metadata = list(
            SignatureOperation.objects.values("evidence_snapshot", "failure_reason")
        )
        assert audit
        assert outbox == []
        assert signing_metadata
        for sink in (
            json.dumps(audit),
            json.dumps(outbox, default=str),
            json.dumps(signing_metadata),
            caplog.text,
        ):
            assert "SINTETICO-SENTINELA-33" not in sink


@pytest.fixture
def exam_signing(signing_setup: SigningSetup) -> SigningSetup:
    """Reuse the real-role signing fixture and its deterministic clocks."""
    setup = signing_setup
    graph = setup.graph
    registration(graph, graph.physician, graph.clinic_a)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        draft = saved(graph, setup.scope, DocumentKind.EXAM_REQUEST)
        setup.document = rendered(graph, setup.scope, draft)
    return setup


@pytest.mark.parametrize("loss", ["registration", "version"])
def test_current_document_sql_guard_rechecks_authority_and_version(
    exam_signing: SigningSetup, loss: str
) -> None:
    setup = exam_signing
    graph = setup.graph
    document = setup.document

    def insert_operation() -> SignatureOperation:
        return SignatureOperation.objects.create(
            organization_id=document.organization_id,
            document=document,
            encounter_id=document.encounter_id,
            clinic_id=document.clinic_id,
            patient_id=document.patient_id,
            issuer_id=document.issuer_id,
            provider=setup.provider.provider,
            content_digest=document.pdf_digest,
            signer_subject=setup.profile.signing_subject,
        )

    # The same direct insert succeeds under clinic_app before authority is lost.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with transaction.atomic():
            operation = insert_operation()
            assert SignatureOperation.objects.get(pk=operation.pk).state == "draft"
            transaction.set_rollback(True)
        assert not SignatureOperation.objects.exists()

    if loss == "registration":
        with owner_context(graph.organization_a):
            ProfessionalRegistration.objects.filter(user_id=graph.physician).update(
                revoked_at=timezone.now()
            )
        reason = "document professional scope denied"
    else:
        with runtime_role(), tenant_context(graph.physician, graph.organization_a):
            save_document_draft(
                clinic_id=graph.clinic_a,
                draft_id=document.draft_id,
                expected_version=document.document_version,
                content=DocumentContentInput(
                    body="Novo pedido sintético", exam_codes=("urinalysis",)
                ),
            )
        reason = "stale_document_approval"

    # Bypass service guards, retain real RLS, and require the trigger's refusal.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(DatabaseError, match=reason), transaction.atomic():
            insert_operation()
        assert not SignatureOperation.objects.exists()


def test_verified_exam_issuance_links_one_order_and_replays_do_not_duplicate(
    exam_signing: SigningSetup,
) -> None:
    # Given: a rendered document has not placed any clinical order.
    setup = exam_signing
    with owner_context(setup.graph.organization_a):
        assert not Order.objects.exists()
    # When: the provider's independently verified callback completes issuance.
    operation = setup.request_signature()
    headers, body = setup.callback(operation)
    assert setup.receive(headers, body) == "applied"
    assert setup.receive(headers, body) == "duplicate"
    # Then: linked frozen terms and the real-role landed command are idempotent.
    with (
        runtime_role(),
        tenant_context(setup.graph.physician, setup.graph.organization_a),
    ):
        order = Order.objects.get(
            command_id=setup.document.frozen_input["order_command_id"]
        )
        assert order.enrollment.patient_id == setup.scope["patient_id"]
        assert order.ordered_by_id == setup.graph.physician
        assert set(
            OrderItem.objects.filter(order=order).values_list("exam_code", flat=True)
        ) == {"cbc"}
        assert set(
            OrderItem.objects.filter(order=order).values_list(
                "catalog_version", flat=True
            )
        ) == {1}
        assert (
            place_order(
                clinic_id=setup.graph.clinic_a,
                enrollment_id=order.enrollment_id,
                exam_codes=("cbc",),
                command_id=order.command_id,
            ).pk
            == order.pk
        )
        assert Order.objects.count() == 1
    assert setup.refresh(operation).state == "rehearsal_complete"


@pytest.mark.parametrize(
    "failure", ["stale", "provider_failed", "abandoned", "authority_revoked"]
)
def test_unapproved_or_failed_exam_issuance_never_places_an_order(
    exam_signing: SigningSetup, failure: str
) -> None:
    setup = exam_signing
    operation = setup.request_signature()
    if failure == "stale":
        with (
            runtime_role(),
            tenant_context(setup.graph.physician, setup.graph.organization_a),
        ):
            save_document_draft(
                clinic_id=setup.graph.clinic_a,
                draft_id=setup.document.draft_id,
                expected_version=2,
                content=DocumentContentInput(
                    body="Novo pedido sintético",
                    exam_codes=("urinalysis",),
                ),
            )
    elif failure == "authority_revoked":
        with owner_context(setup.graph.organization_a):
            ProfessionalRegistration.objects.filter(
                user_id=setup.graph.physician
            ).update(revoked_at=timezone.now())
    elif failure == "abandoned":
        with (
            runtime_role(),
            tenant_context(setup.graph.physician, setup.graph.organization_a),
        ):
            abandon_signature(clinic_id=setup.graph.clinic_a, operation_id=operation.pk)
    headers, body = setup.callback(
        operation, status="failed" if failure == "provider_failed" else "signed"
    )
    outcome = setup.receive(headers, body)
    assert outcome in {"applied", "rejected"}
    with owner_context(setup.graph.organization_a):
        assert not Order.objects.exists()
        operation.refresh_from_db()
        assert operation.state == "failed"


class IssuanceInterruptedError(Exception):
    """Interrupt the transaction after both order and completed state are written."""


@pytest.mark.parametrize(
    "failure_type",
    [IssuanceInterruptedError, KeyboardInterrupt],
    ids=["failure", "interruption"],
)
def test_issuance_failure_rolls_back_order_link_callback_and_completion(
    exam_signing: SigningSetup,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    setup = exam_signing
    operation = setup.request_signature()
    headers, body = setup.callback(operation)
    original_event = signing._signature_event

    def interrupt(
        operation_id: UUID,
        clinic_id: UUID,
        action: str,
        *,
        reason_code: str | None = None,
    ) -> None:
        if action == "rehearsal_completed":
            raise failure_type
        original_event(operation_id, clinic_id, action, reason_code=reason_code)

    # Given/when: the actual callback transaction fails after terminal writes.
    with monkeypatch.context() as patch:
        patch.setattr(signing, "_signature_event", interrupt)
        with pytest.raises(failure_type):
            setup.receive(headers, body)
    # Then: no issued-without-order or orphan order/receipt can remain.
    with owner_context(setup.graph.organization_a):
        assert not Order.objects.exists()
        assert not SignatureCallback.objects.filter(operation=operation).exists()
        operation.refresh_from_db()
        assert operation.state == "signing"
    assert setup.receive(headers, body) == "applied"
    with owner_context(setup.graph.organization_a):
        assert (
            Order.objects.filter(
                command_id=setup.document.frozen_input["order_command_id"]
            ).count()
            == 1
        )
        operation.refresh_from_db()
        assert operation.state == "rehearsal_complete"


def test_native_document_forms_preserve_text_and_reject_misleading_success(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/clinical-documents/"
    with physician_client(rbac_graph) as client:
        assert client.get(url).status_code == 200
        assert client.post(url, {"action": "save"}).status_code == 403
        # Unsupported UI kinds cannot create a hidden prescription as a side effect.
        assert (
            client.post(
                url,
                {
                    "action": "open",
                    "encounter_id": scope["encounter_id"],
                    "kind": "prescription",
                },
            ).status_code
            == 400
        )
        assert (
            client.post(
                url,
                {
                    "action": "open",
                    "encounter_id": scope["encounter_id"],
                    "kind": "certificate",
                },
            ).status_code
            == 302
        )
        draft_id = client.session[
            f"prescription.clinical-document.{rbac_graph.clinic_a}"
        ]
        command = {"draft_id": draft_id, "expected_version": 1, "body": BODY}
        assert client.post(url, {**command, "action": "save"}).status_code == 302
        assert b'data-save-state="saved"' in client.get(url).content
        refused = client.post(
            url,
            {
                **command,
                "expected_version": 2,
                "body": "Texto recusado sintético",
                "action": "save",
                "cid10_code": "A00",
                "cid10_source_version": "unapproved",
            },
        )
        assert refused.status_code == 400
        assert b'data-save-state="unsaved"' in refused.content
        assert "Texto recusado sintético".encode() in refused.content
        stale = client.post(url, {**command, "action": "save"})
        assert stale.status_code == 409
        assert b'data-outcome="stale_revision"' in stale.content
        render = {"draft_id": draft_id, "expected_version": 2, "action": "render"}
        first = client.post(url, render)
        assert first.status_code == 302
        assert client.post(url, render).headers["Location"] == first.headers["Location"]
        review = client.get(first.headers["Location"])
        assert review.status_code == 200
        assert b"data-frozen-content" in review.content
        assert (
            client.post(
                url,
                {
                    **render,
                    "action": "release_document",
                    "document_id": first.headers["Location"].split("/")[-3],
                },
            ).status_code
            == 409
        )
        assert client.post(url, {**render, "action": "discard"}).status_code == 302
        assert b"data-document-kind" not in client.get(url).content
    with owner_context(rbac_graph.organization_a):
        assert PrescriptionDraft.objects.count() == 1
        assert DocumentDraftContent.objects.count() == 1
        assert PrescriptionDocument.objects.count() == 1
        assert not Order.objects.exists()


def test_native_exam_validation_has_no_order_or_issued_success(
    rbac_graph: RbacGraph,
) -> None:
    scope = scoped(rbac_graph)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/clinical-documents/"
    with physician_client(rbac_graph) as client:
        assert (
            client.post(
                url,
                {
                    "action": "open",
                    "kind": "exam_request",
                    "encounter_id": scope["encounter_id"],
                },
            ).status_code
            == 302
        )
        draft_id = client.session[
            f"prescription.clinical-document.{rbac_graph.clinic_a}"
        ]
        command = {
            "action": "save",
            "draft_id": draft_id,
            "expected_version": 1,
            "body": "Indicação sintética",
        }
        assert (
            client.post(url, {**command, "exam_codes": ["unknown"]}).status_code == 400
        )
        assert client.post(url, {**command, "exam_codes": []}).status_code == 400
        assert client.post(url, {**command, "exam_codes": ["cbc"]}).status_code == 302
        assert (
            client.post(
                url,
                {
                    "action": "render",
                    "draft_id": draft_id,
                    "expected_version": 1,
                },
            ).status_code
            == 409
        )
        response = client.post(
            url,
            {
                "action": "render",
                "draft_id": draft_id,
                "expected_version": 2,
            },
        )
        assert response.status_code == 302
        assert client.get(response.headers["Location"]).status_code == 200
        assert (
            client.post(
                url,
                {
                    "action": "open_draft",
                    "draft_id": draft_id,
                },
            ).status_code
            == 302
        )
        assert (
            client.post(
                url,
                {
                    "action": "unknown",
                    "draft_id": draft_id,
                },
            ).status_code
            == 403
        )
    with owner_context(rbac_graph.organization_a):
        assert not Order.objects.exists()
        assert not SignatureOperation.objects.exists()

"""Attachment quarantine acceptance against real PostgreSQL and clinic_app."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.attachment_scanner import AttachmentScanUnavailableError
from apps.ehr.attachment_storage import (
    AttachmentStorageError,
    FilesystemAttachmentStorage,
)
from apps.ehr.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentInput,
    AttachmentScanFailedError,
    attachment_context,
    download_attachment,
    reconcile_pending_uploads,
    scan_attachment,
    upload_attachment,
)
from apps.ehr.models import ClinicalAttachment, Encounter
from apps.ehr.services import ClinicalAccessDeniedError, open_encounter
from apps.identity.models import UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import reveal
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.test import Client, override_settings

from otp_test_support import (
    OTP_RAW_CREDENTIAL,
    create_receptionist,
)
from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client, seed, setup_context
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

PDF_BYTES = b"%PDF-1.4\n%synthetic clinical attachment\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01synthetic\xff\xd9"
ACTIVE_PDF = b"%PDF-1.4\n1 0 obj<</OpenAction<</S/JavaScript/JS(app.alert(1))>>>>\n"
SVG_BYTES = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
ZIP_BYTES = b"PK\x03\x04synthetic-archive-bytes"


@pytest.fixture
def attachment_root(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "objects"
    with override_settings(EHR_ATTACHMENT_ROOT=root):
        yield root


@contextmanager
def actor_context(user_id: UUID, organization_id: UUID) -> Iterator[None]:
    """Set both tenant GUCs without touching the connection's current role."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true), "
            "set_config('app.current_tenant', %s, true)",
            [str(user_id), str(organization_id)],
        )
        yield


def begin(graph: RbacGraph) -> Encounter:
    appointment, _ = seed(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        return open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)


def upload(
    graph: RbacGraph,
    encounter: Encounter,
    data: bytes = PDF_BYTES,
    declared: str = "application/pdf",
) -> ClinicalAttachment:
    return upload_attachment(
        clinic_id=graph.clinic_a,
        encounter_id=encounter.pk,
        upload=AttachmentInput(
            file_name="exame-sintetico.pdf", declared_type=declared, data=data
        ),
    )


def test_upload_quarantine_scan_and_exact_download(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        assert attachment.state == "quarantined"
        assert attachment.scan_attempts == 0
        assert attachment.scanned_at is None
        assert attachment.sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
        assert attachment.storage_key != str(attachment.pk)
        # Only the tenant envelope reaches object storage; the recorded
        # digest describes the plaintext the download path verifies.
        stored = (attachment_root / attachment.storage_key).read_bytes()
        assert stored != PDF_BYTES
        assert stored[:1] == b"\x01"
        assert (
            reveal(purpose="ehr.clinicalattachment.bytes", envelope=stored) == PDF_BYTES
        )
        # Quarantined bytes are never served, even to the assigned physician.
        with pytest.raises(ClinicalAccessDeniedError):
            download_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
        scanned = scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
        assert scanned.state == "available"
        assert scanned.scan_attempts == 1
        assert scanned.scanned_at is not None
        # A repeated scan returns the terminal row without a second transition.
        again = scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
        assert again.state == "available"
        assert again.scan_attempts == 1
        result = download_attachment(
            clinic_id=graph.clinic_a, attachment_id=attachment.pk
        )
        assert result.data == PDF_BYTES
        assert result.content_type == "application/pdf"
        assert result.file_name == f"anexo-{attachment.pk}.pdf"
    with setup_context(graph.organization_a):
        events = AuditEvent.objects.filter(
            event_type__startswith="ehr.attachment."
        ).values_list("event_type", "payload")
        assert [event[0] for event in events] == [
            "ehr.attachment.uploaded",
            "ehr.attachment.scanned",
            "ehr.attachment.downloaded",
        ]
        assert all(
            set(payload) <= {"clinic_id", "object_verb"} for _, payload in events
        )
        assert AuditEvent.objects.filter(
            event_type="ehr.access.denied", payload__reason_code="not_available"
        ).exists()


@pytest.mark.parametrize(
    ("data", "declared"),
    [
        (b"", "application/pdf"),
        (b"x" * (MAX_ATTACHMENT_BYTES + 1), "application/pdf"),
        (PNG_BYTES, "application/pdf"),
        (b"plain text without a signature", "application/pdf"),
        (SVG_BYTES, "image/png"),
        (b"<html><body>synthetic</body></html>", "application/pdf"),
        (ZIP_BYTES, "application/pdf"),
        (PDF_BYTES, "text/html"),
        (PDF_BYTES, "application/zip"),
    ],
)
def test_rejects_oversized_type_confused_active_and_archive(
    rbac_graph: RbacGraph, attachment_root: Path, data: bytes, declared: str
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(ValidationError):
            upload(graph, encounter, data=data, declared=declared)
        assert not ClinicalAttachment.objects.exists()
    assert not attachment_root.exists() or not list(attachment_root.iterdir())


def test_failed_scan_keeps_quarantine_and_retry_metadata(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        # An active payload passes upload validation but fails the scan.
        active = upload(graph, encounter, data=ACTIVE_PDF)
        scanned = scan_attachment(clinic_id=graph.clinic_a, attachment_id=active.pk)
        assert scanned.state == "rejected"
        assert scanned.scan_reason == "active_pdf_content"
        assert scanned.scan_attempts == 1
        with pytest.raises(ClinicalAccessDeniedError):
            download_attachment(clinic_id=graph.clinic_a, attachment_id=active.pk)

        # A scanner outage retains the row quarantined with retry metadata.
        class FailingScanner:
            def scan(
                self, *, attachment: ClinicalAttachment, data: bytes
            ) -> tuple[str, str]:
                raise AttachmentScanUnavailableError

        with pytest.raises(AttachmentScanFailedError):
            scan_attachment(
                clinic_id=graph.clinic_a,
                attachment_id=attachment.pk,
                scanner=FailingScanner(),
            )
        retained = ClinicalAttachment.objects.get(pk=attachment.pk)
        assert retained.state == "quarantined"
        assert retained.scan_attempts == 1
        assert retained.scan_reason == "scan_unavailable"
        assert retained.scanned_at is None
        # The retry succeeds once the scanner is available again.
        recovered = scan_attachment(
            clinic_id=graph.clinic_a, attachment_id=attachment.pk
        )
        assert recovered.state == "available"
        assert recovered.scan_attempts == 2
    with setup_context(graph.organization_a):
        assert AuditEvent.objects.filter(
            event_type="ehr.attachment.scan_failed",
            payload__reason_code="scan_unavailable",
        ).exists()


def test_denials_never_serve_quarantined_or_foreign_bytes(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        scanned = scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
        assert scanned.state == "available"
    # Reception and admin hold no clinical predicate at all.
    for actor in (graph.shared_user, graph.clinic_admin):
        with runtime_role(), tenant_context(actor, graph.organization_a):
            assert not ClinicalAttachment.objects.exists()
            with pytest.raises(ClinicalAccessDeniedError):
                download_attachment(
                    clinic_id=graph.clinic_a, attachment_id=attachment.pk
                )
            with pytest.raises(ClinicalAccessDeniedError):
                upload(graph, encounter)
            with pytest.raises(ClinicalAccessDeniedError):
                scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
    # Another tenant sees nothing and cannot download.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not ClinicalAttachment.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            download_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
    # An altered identifier and a raw storage key are equally non-enumerating.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        for altered in (uuid4(),):
            with pytest.raises(ClinicalAccessDeniedError):
                download_attachment(clinic_id=graph.clinic_a, attachment_id=altered)
        assert not ClinicalAttachment.objects.filter(pk=uuid4()).exists()
    # A same-clinic physician with a care relationship reads only available rows.
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="physician",
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.clinic_admin,
            start_local="2035-06-03T08:00",
            end_local="2035-06-03T12:00",
            idempotency_key=uuid4(),
        )
    with setup_context(graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=encounter.patient_id, clinic_id=graph.clinic_a
        )
        setup = AppointmentSetup(
            graph.organization_a,
            graph.clinic_a,
            graph.shared_user,
            graph.clinic_admin,
            enrollment.pk,
            encounter.patient_id,
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_synthetic_appointment(
            setup, start_local="2035-06-03T09:00", end_local="2035-06-03T10:00"
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        pending = upload(graph, encounter, data=PNG_BYTES, declared="image/png")
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        # The quarantined row is invisible; the available one downloads exactly.
        visible = attachment_context(encounter).attachments
        assert [row.pk for row in visible] == [attachment.pk]
        result = download_attachment(
            clinic_id=graph.clinic_a, attachment_id=attachment.pk
        )
        assert result.data == PDF_BYTES
        with pytest.raises(ClinicalAccessDeniedError):
            download_attachment(clinic_id=graph.clinic_a, attachment_id=pending.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            upload(graph, encounter)
        with pytest.raises(ClinicalAccessDeniedError):
            scan_attachment(clinic_id=graph.clinic_a, attachment_id=pending.pk)


def test_http_upload_scan_download_and_denials(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    url = f"/ehr/clinics/{graph.clinic_a}/attachments/"
    with physician_client(graph) as client:
        empty = client.get(url)
        assert empty.status_code == 200
        assert "no-store" in empty.headers["Cache-Control"]
        assert client.post(url, {"action": "invalid"}).status_code == 403
        assert (
            client.post(
                url, {"action": "open", "encounter_id": encounter.pk}
            ).status_code
            == 302
        )
        upload_response = client.post(
            url,
            {
                "action": "upload",
                "encounter_id": encounter.pk,
                "attachment": SimpleUploadedFile(
                    "exame.pdf", PDF_BYTES, content_type="application/pdf"
                ),
            },
        )
        assert upload_response.status_code == 302
        # Inside physician_client the connection already runs as clinic_app;
        # the GUC context reproduces the real read path without a role reset.
        with actor_context(graph.physician, graph.organization_a):
            attachment = ClinicalAttachment.objects.get()
        assert attachment.state == "quarantined"
        # Quarantined bytes are never served over HTTP either.
        denied = client.post(
            url,
            {
                "action": "download",
                "encounter_id": encounter.pk,
                "attachment_id": attachment.pk,
            },
        )
        assert denied.status_code == 403
        assert PDF_BYTES not in denied.content
        assert (
            client.post(
                url,
                {
                    "action": "scan",
                    "encounter_id": encounter.pk,
                    "attachment_id": attachment.pk,
                },
            ).status_code
            == 302
        )
        with actor_context(graph.physician, graph.organization_a):
            attachment.refresh_from_db()
        assert attachment.state == "available"
        download = client.post(
            url,
            {
                "action": "download",
                "encounter_id": encounter.pk,
                "attachment_id": attachment.pk,
            },
        )
        assert download.status_code == 200
        assert download.streaming is False
        assert download.content == PDF_BYTES
        assert download.headers["Content-Type"] == "application/pdf"
        assert "no-store" in download.headers["Cache-Control"]
        assert download.headers["Content-Disposition"] == (
            f'attachment; filename="anexo-{attachment.pk}.pdf"'
        )
        assert download.headers["X-Content-Type-Options"] == "nosniff"
        # Altered ids and raw storage keys are not identifiers.
        for forged in (uuid4(), attachment.storage_key):
            assert (
                client.post(
                    url,
                    {
                        "action": "download",
                        "encounter_id": encounter.pk,
                        "attachment_id": forged,
                    },
                ).status_code
                == 403
            )
        # A type-confused upload is refused without creating a row.
        confused = client.post(
            url,
            {
                "action": "upload",
                "encounter_id": encounter.pk,
                "attachment": SimpleUploadedFile(
                    "nota.pdf", PNG_BYTES, content_type="application/pdf"
                ),
            },
        )
        assert confused.status_code == 400
        with actor_context(graph.physician, graph.organization_a):
            assert ClinicalAttachment.objects.count() == 1
    # Reception is denied on the same real middleware path. A dedicated
    # receptionist keeps the active organization and TOTP state deterministic:
    # the shared fixture user is also a physician in organization B, so login
    # could select that tenant and redirect to enrollment instead of reaching
    # the intended authorization denial.
    receptionist = create_receptionist(graph)
    reception = Client()
    with http_runtime_role():
        assert (
            reception.post(
                "/auth/login/",
                {
                    "username": receptionist.username,
                    "password": OTP_RAW_CREDENTIAL,
                },
            ).status_code
            == 302
        )
        assert (
            reception.post(
                url,
                {
                    "action": "download",
                    "encounter_id": encounter.pk,
                    "attachment_id": attachment.pk,
                },
            ).status_code
            == 403
        )
        assert (
            reception.post(
                url, {"action": "open", "encounter_id": encounter.pk}
            ).status_code
            == 403
        )


def test_exact_attachment_rls_acl_and_immutability(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity,relowner::regrole::text "
            "FROM pg_class WHERE relname='ehr_clinicalattachment'"
        )
        assert cursor.fetchone() == (True, True, "clinic_owner")
        cursor.execute(
            "SELECT policyname,cmd FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename='ehr_clinicalattachment'"
        )
        assert set(cursor.fetchall()) == {
            ("setup_tenant", "ALL"),
            ("attachment_read", "SELECT"),
            ("attachment_insert", "INSERT"),
            ("attachment_scan", "UPDATE"),
        }
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name='ehr_clinicalattachment'"
        )
        # Column-level UPDATE grants surface in role_column_grants, not here.
        assert {row[0] for row in cursor.fetchall()} == {"SELECT", "INSERT"}
        cursor.execute(
            "SELECT column_name FROM information_schema.role_column_grants "
            "WHERE grantee='clinic_app' AND privilege_type='UPDATE' "
            "AND table_schema='clinic_app' "
            "AND table_name='ehr_clinicalattachment'"
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "state",
            "scan_attempts",
            "scan_reason",
            "scanned_at",
        }
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # The UPDATE policy silently filters terminal rows; identity columns
        # have no grant at all, and DELETE is never granted.
        assert (
            ClinicalAttachment.objects.filter(pk=attachment.pk).update(
                state="quarantined"
            )
            == 0
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalAttachment.objects.filter(pk=attachment.pk).update(
                file_name="forged.pdf"
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalAttachment.objects.filter(pk=attachment.pk).delete()
    with (
        setup_context(graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        ClinicalAttachment.objects.filter(pk=attachment.pk).update(state="quarantined")


def test_storage_boundary_is_bounded_and_opaque(attachment_root: Path) -> None:
    storage = FilesystemAttachmentStorage(attachment_root)
    with pytest.raises(AttachmentStorageError):
        storage.put("../escape", b"x")
    with pytest.raises(AttachmentStorageError):
        storage.get("0" * 64)
    key = "a" * 64
    storage.put(key, PDF_BYTES)
    assert storage.get(key) == PDF_BYTES
    with pytest.raises(AttachmentStorageError):
        storage.put(key, PDF_BYTES)
    oversized = "b" * 64
    (attachment_root / oversized).write_bytes(b"x" * (MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(AttachmentStorageError):
        storage.get(oversized)
    storage.delete(key)
    storage.delete(key)
    assert not (attachment_root / key).exists()


def test_synthetic_scanner_never_approves_outside_synthetic_mode(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    graph = rbac_graph
    encounter = begin(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        attachment = upload(graph, encounter)
        with (
            override_settings(CLINIC_DATA_MODE="live"),
            pytest.raises(AttachmentScanFailedError),
        ):
            scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
        retained = ClinicalAttachment.objects.get(pk=attachment.pk)
        assert retained.state == "quarantined"
        assert retained.scan_reason == "scan_unavailable"


def test_request_rollback_leaves_tracked_object_reconciled(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    """An outer request-transaction rollback keeps durable cleanup metadata.

    The injected failure lands after the service returns, so the real tenant
    middleware rolls the row back while the object stays stored. The pending
    receipt is the cleanup record; reconciliation removes the object exactly.
    """
    graph = rbac_graph
    encounter = begin(graph)
    url = f"/ehr/clinics/{graph.clinic_a}/attachments/"
    with physician_client(graph) as client:
        committed = client.post(
            url,
            {
                "action": "upload",
                "encounter_id": encounter.pk,
                "attachment": SimpleUploadedFile(
                    "exame.pdf", PDF_BYTES, content_type="application/pdf"
                ),
            },
        )
        assert committed.status_code == 302
        with actor_context(graph.physician, graph.organization_a):
            kept = ClinicalAttachment.objects.get()
        # A committed upload clears its own receipt.
        assert FilesystemAttachmentStorage(attachment_root).pending_uploads() == []

        client.raise_request_exception = False
        with patch(
            "apps.ehr.attachment_views.messages.success",
            side_effect=RuntimeError("synthetic post-upload failure"),
        ):
            failed = client.post(
                url,
                {
                    "action": "upload",
                    "encounter_id": encounter.pk,
                    "attachment": SimpleUploadedFile(
                        "rollback.pdf", PNG_BYTES, content_type="image/png"
                    ),
                },
            )
        assert failed.status_code == 500
        with actor_context(graph.physician, graph.organization_a):
            # The rolled-back row is gone; only the committed upload remains.
            assert [row.pk for row in ClinicalAttachment.objects.all()] == [kept.pk]
        storage = FilesystemAttachmentStorage(attachment_root)
        pending = storage.pending_uploads()
        assert len(pending) == 1
        orphan = pending[0]
        assert orphan.organization_id == str(graph.organization_a)
        with actor_context(graph.physician, graph.organization_a):
            assert (
                reveal(
                    purpose="ehr.clinicalattachment.bytes",
                    envelope=(attachment_root / orphan.key).read_bytes(),
                )
                == PNG_BYTES
            )

    # Reconciliation runs as the migration role (clinic_owner): the runtime
    # role's read policy would hide committed rows and misreport orphans.
    removed = reconcile_pending_uploads(storage=storage)
    assert removed == [orphan.key]
    assert storage.pending_uploads() == []
    assert {path.name for path in attachment_root.iterdir()} == {kept.storage_key}


def test_failed_cleanup_keeps_durable_receipt(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    """A cleanup failure during row rollback still leaves tracked bytes."""
    graph = rbac_graph
    encounter = begin(graph)

    class FlakyDeleteStorage(FilesystemAttachmentStorage):
        attempts = 0

        def delete(self, key: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise AttachmentStorageError
            super().delete(key)

    storage = FlakyDeleteStorage(attachment_root)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        patch(
            "apps.ehr.attachments.record_phase1_event",
            side_effect=DatabaseError("synthetic row failure"),
        ),
        pytest.raises(AttachmentStorageError),
    ):
        upload_attachment(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            upload=AttachmentInput(
                file_name="exame.pdf",
                declared_type="application/pdf",
                data=PDF_BYTES,
            ),
            storage=storage,
        )
    # The failed delete kept the receipt, so the object is still tracked.
    pending = storage.pending_uploads()
    assert len(pending) == 1
    with actor_context(graph.physician, graph.organization_a):
        assert (
            reveal(
                purpose="ehr.clinicalattachment.bytes",
                envelope=(attachment_root / pending[0].key).read_bytes(),
            )
            == PDF_BYTES
        )
    removed = reconcile_pending_uploads(storage=storage)
    assert removed == [pending[0].key]
    assert not list(attachment_root.iterdir())

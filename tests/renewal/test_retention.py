"""Retention, holds, releases and export acceptance on real PostgreSQL."""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.finalization import amend_document, finalize_version
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.models import User, UserClinicRole
from apps.intake.models import PatientSession
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.retention.models import (
    LegalHold,
    RecordExport,
    RecordRelease,
    RetentionPolicy,
)
from apps.retention.services import (
    RetentionAccessDeniedError,
    RetentionConflictError,
    _released_for_patient_staff,
    approve_policy,
    export_patient_records,
    export_staff_records,
    patient_released_records,
    place_hold,
    propose_policy,
    record_disposition,
    release_hold,
    release_version,
    request_disposal,
    retire_policy,
    revoke_release,
    verify_export_package,
    verify_stored_export,
)
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import Client

from otp_test_support import (
    create_totp_device,
    fixed_otp_time,
    token_for,
)
from otp_test_support import runtime_role as http_runtime_role
from patient_service_support import runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL
from renewal.test_amendments import AMENDED, saved_draft
from renewal.test_encounters import physician_client, seed, setup_context
from stepup_test_support import create_role_actor, verified_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any
    from uuid import UUID

    from apps.ehr.models import SpecialtyTemplate
    from apps.scheduling.models import Appointment
    from django.http import HttpRequest

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

TABLES = {
    "retention_retentionpolicy",
    "retention_legalhold",
    "retention_recordrelease",
    "retention_recordexport",
}


@contextmanager
def staff_client(user_id: UUID) -> Iterator[Client]:
    """Sign in one staff user through the real login and TOTP verify flow."""
    user = User.objects.get(pk=user_id)
    device = create_totp_device(user_id, confirmed=True)
    client = Client()
    with http_runtime_role(), fixed_otp_time():
        assert (
            client.post(
                "/auth/login/",
                {"username": user.username, "password": RBAC_RAW_CREDENTIAL},
            ).status_code
            == 302
        )
        assert (
            client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            ).status_code
            == 302
        )
        yield client


def finalized(
    graph: RbacGraph,
    appointment: Appointment,
    template: SpecialtyTemplate,
    request: HttpRequest,
) -> ClinicalDocumentVersion:
    """Produce one finalized version through the real service path."""
    version = saved_draft(graph, appointment, template)
    return finalize_version(
        clinic_id=graph.clinic_a,
        version_id=version.pk,
        expected_revision=2,
        request=request,
    )


def admin(graph: RbacGraph) -> User:
    """Create one clinic admin for clinic A."""
    return create_role_actor(graph, UserClinicRole.Role.CLINIC_ADMIN)


def test_policy_lifecycle_is_versioned_and_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        policy = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            retention_days=365,
        )
        assert policy.state == "proposed"
        assert policy.version == 1
        # A proposed policy never makes a record eligible.
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        decision = record_disposition(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
        )
        assert decision.allowed is False
        assert decision.reason_code == "no_approved_policy"
        with pytest.raises(RetentionAccessDeniedError):
            request_disposal(
                clinic_id=graph.clinic_a,
                record_class="ehr.document_version",
                record_id=version.pk,
            )
        approved = approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        assert approved.state == "approved"
        assert approved.approved_by_id == manager.pk
        # A second version supersedes the approved one atomically.
        newer = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            retention_days=None,
        )
        assert newer.version == 2
        approve_policy(clinic_id=graph.clinic_a, policy_id=newer.pk)
        states = list(
            RetentionPolicy.objects.order_by("version").values_list("version", "state")
        )
        assert states == [(1, "retired"), (2, "approved")]
        # An indefinite approved policy still denies disposal.
        decision = record_disposition(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
        )
        assert decision.allowed is False
        assert decision.reason_code == "indefinite_retention"
        # Retiring the last approved policy closes the door again.
        retire_policy(clinic_id=graph.clinic_a, policy_id=newer.pk)
        assert (
            record_disposition(
                clinic_id=graph.clinic_a,
                record_class="ehr.document_version",
                record_id=version.pk,
            ).reason_code
            == "no_approved_policy"
        )
    with setup_context(graph.organization_a):
        verbs = AuditEvent.objects.filter(
            event_type__startswith="retention.policy."
        ).values_list("event_type", flat=True)
        assert sorted(verbs) == [
            "retention.policy.approved",
            "retention.policy.approved",
            "retention.policy.proposed",
            "retention.policy.proposed",
            "retention.policy.retired",
            "retention.policy.retired",
        ]
        assert AuditEvent.objects.filter(
            event_type="retention.access.denied",
            payload__reason_code="no_approved_policy",
        ).exists()


def test_policy_denials_and_raw_guards(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        policy = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.encounter",
            retention_days=30,
        )
    # Physicians and receptionists cannot propose, approve or retire.
    for actor in (graph.physician, graph.shared_user):
        with runtime_role(), tenant_context(actor, graph.organization_a):
            with pytest.raises(RetentionAccessDeniedError):
                propose_policy(
                    clinic_id=graph.clinic_a,
                    record_class="ehr.encounter",
                    retention_days=1,
                )
            with pytest.raises(RetentionAccessDeniedError):
                approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
            with pytest.raises(RetentionAccessDeniedError):
                retire_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
    # The other tenant sees nothing and cannot act.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not RetentionPolicy.objects.exists()
        with pytest.raises(RetentionAccessDeniedError):
            approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        # Raw inserts cannot skip the version chain or the proposed state.
        with pytest.raises(DatabaseError), transaction.atomic():
            RetentionPolicy.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                record_class="ehr.encounter",
                version=9,
                retention_days=1,
                proposed_by_id=manager.pk,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            RetentionPolicy.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                record_class="ehr.encounter",
                version=2,
                state="approved",
                retention_days=1,
                proposed_by_id=manager.pk,
                approved_by_id=manager.pk,
                approved_at=policy.created_at,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            RetentionPolicy.objects.filter(pk=policy.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            RetentionPolicy.objects.filter(pk=policy.pk).update(retention_days=1)
        assert RetentionPolicy.objects.get(pk=policy.pk).state == "proposed"


def test_hold_blocks_disposal_and_release_is_recorded(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    manager = admin(graph)
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        # An approved zero-day policy makes the record eligible only in theory.
        policy = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            retention_days=0,
        )
        approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        decision = request_disposal(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
        )
        assert decision.allowed is True
        assert decision.reason_code == "eligible"
        hold = place_hold(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
            authority="Ministério Público sintético",
            reason="Investigação sintética",
        )
        assert hold.placed_by_id == manager.pk
        # An identical active hold is idempotent.
        again = place_hold(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
            authority="Ministério Público sintético",
            reason="Investigação sintética",
        )
        assert again.pk == hold.pk
        assert LegalHold.objects.count() == 1
        with pytest.raises(RetentionAccessDeniedError):
            request_disposal(
                clinic_id=graph.clinic_a,
                record_class="ehr.document_version",
                record_id=version.pk,
            )
        # Raw deletion of the hold itself is forbidden too.
        with pytest.raises(DatabaseError), transaction.atomic():
            LegalHold.objects.filter(pk=hold.pk).delete()
        released = release_hold(
            clinic_id=graph.clinic_a,
            hold_id=hold.pk,
            authority="Desembargador sintético",
            reason="Encerramento sintético",
        )
        assert released.released_at is not None
        assert released.release_authority == "Desembargador sintético"
        # Releasing twice returns the recorded release unchanged.
        repeat = release_hold(
            clinic_id=graph.clinic_a,
            hold_id=hold.pk,
            authority="Outra autoridade",
            reason="Outro motivo",
        )
        assert repeat.release_authority == "Desembargador sintético"
        assert (
            request_disposal(
                clinic_id=graph.clinic_a,
                record_class="ehr.document_version",
                record_id=version.pk,
            ).allowed
            is True
        )
    # The row still exists: eligibility never deletes, and raw deletion of the
    # held record hits the binding trigger even for the owner role.
    with setup_context(graph.organization_a):
        assert ClinicalDocumentVersion.objects.filter(pk=version.pk).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).delete()
        assert (
            AuditEvent.objects.filter(event_type="retention.hold.placed").count() == 1
        )
        assert (
            AuditEvent.objects.filter(event_type="retention.hold.released").count() == 1
        )
        assert AuditEvent.objects.filter(
            event_type="retention.access.denied", payload__reason_code="held"
        ).exists()
        assert (
            AuditEvent.objects.filter(event_type="retention.disposal.evaluated").count()
            == 2
        )


def test_hold_denials_and_foreign_records(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    manager = admin(graph)
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        # Unknown classes, unknown records and foreign clinics all fail closed.
        for record_class, record_id in (
            ("ehr.unknown", version.pk),
            ("ehr.document_version", uuid4()),
        ):
            with pytest.raises(RetentionAccessDeniedError):
                place_hold(
                    clinic_id=graph.clinic_a,
                    record_class=record_class,
                    record_id=record_id,
                    authority="Autoridade",
                    reason="Motivo",
                )
        with pytest.raises(RetentionAccessDeniedError):
            place_hold(
                clinic_id=graph.clinic_b,
                record_class="ehr.document_version",
                record_id=version.pk,
                authority="Autoridade",
                reason="Motivo",
            )
        assert not LegalHold.objects.exists()
        with pytest.raises(RetentionAccessDeniedError):
            release_hold(
                clinic_id=graph.clinic_a,
                hold_id=uuid4(),
                authority="x",
                reason="y",
            )
    # Non-managers cannot place or release holds at all.
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        hold = place_hold(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
            authority="Autoridade",
            reason="Motivo",
        )
    for actor in (graph.physician, graph.shared_user):
        with runtime_role(), tenant_context(actor, graph.organization_a):
            with pytest.raises(RetentionAccessDeniedError):
                place_hold(
                    clinic_id=graph.clinic_a,
                    record_class="ehr.document_version",
                    record_id=version.pk,
                    authority="Autoridade",
                    reason="Motivo",
                )
            with pytest.raises(RetentionAccessDeniedError):
                release_hold(
                    clinic_id=graph.clinic_a,
                    hold_id=hold.pk,
                    authority="x",
                    reason="y",
                )
            with pytest.raises(RetentionAccessDeniedError):
                request_disposal(
                    clinic_id=graph.clinic_a,
                    record_class="ehr.document_version",
                    record_id=version.pk,
                )
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        assert LegalHold.objects.get(pk=hold.pk).released_at is None


def test_release_revoke_contract(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        draft = saved_draft(graph, appointment, template)
        # Drafts and unknown versions can never be released.
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            release_version(clinic_id=graph.clinic_a, version_id=draft.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            release_version(clinic_id=graph.clinic_a, version_id=uuid4())
        version = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=2,
            request=request,
        )
        release = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert release.patient_id == version.document.encounter.patient_id
        assert release.released_by_id == graph.physician
        # Re-release is idempotent.
        again = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert again.pk == release.pk
        assert RecordRelease.objects.count() == 1
        revoked = revoke_release(clinic_id=graph.clinic_a, release_id=release.pk)
        assert revoked.revoked_at is not None
        assert revoked.revoked_by_id == graph.physician
        # A repeated revoke returns the existing revoked release unchanged.
        repeat = revoke_release(clinic_id=graph.clinic_a, release_id=release.pk)
        assert repeat.revoked_at == revoked.revoked_at
        # Unknown release ids are non-enumerating denials.
        with pytest.raises(RetentionAccessDeniedError):
            revoke_release(clinic_id=graph.clinic_a, release_id=uuid4())
        # A fresh release after revocation is a new row.
        second = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert second.pk != release.pk
    with setup_context(graph.organization_a):
        assert (
            AuditEvent.objects.filter(event_type="ehr.document.released").count() == 2
        )
        assert (
            AuditEvent.objects.filter(event_type="ehr.document.release_revoked").count()
            == 1
        )
        assert AuditEvent.objects.filter(
            event_type="ehr.access.denied", payload__reason_code="not_found"
        ).exists()


def test_release_denied_to_non_assigned(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    # Receptionists, admins and other-clinic physicians cannot release.
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="physician",
        )
    for actor, org in (
        (graph.shared_user, graph.organization_a),
        (graph.clinic_admin, graph.organization_a),
        (graph.shared_user, graph.organization_b),
    ):
        with (
            runtime_role(),
            tenant_context(actor, org),
            pytest.raises(ClinicalAccessDeniedError),
        ):
            release_version(clinic_id=graph.clinic_a, version_id=version.pk)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not RecordRelease.objects.exists()


def _package_parts(data: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        files = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "manifest.json"
        }
    return manifest, files


def test_staff_export_digest_verified_and_scoped(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        patient_id = version.document.encounter.patient_id
        package = export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
        assert package.export.kind == "staff"
        assert package.export.requested_by_id == graph.physician
        assert package.export.record_count == 1
        manifest, files = _package_parts(package.data)
        assert manifest["v"] == "clinic-record-export-v1"
        assert manifest["export_id"] == str(package.export.pk)
        assert manifest["record_count"] == 1
        assert manifest["clinic_id"] == str(graph.clinic_a)
        assert manifest["patient_id"] == str(patient_id)
        entry = manifest["files"][0]
        assert entry["record_id"] == str(version.pk)
        assert entry["content_digest"] == version.content_digest
        assert sha256(files[entry["path"]]).hexdigest() == entry["sha256"]
        document = json.loads(files[entry["path"]])
        assert document["content"]["subjective"] == "Relato sintético"
        assert document["state"] == "finalized"
        assert document["author_label"]
        # The stored receipt verifies the same package.
        stored = verify_stored_export(
            clinic_id=graph.clinic_a,
            export_id=package.export.pk,
            data=package.data,
        )
        assert stored.ok is True
        assert stored.manifest_digest == package.export.manifest_digest
        # An amended, re-released lineage exports both versions in order.
        from apps.ehr.services import record_clinical_note  # noqa: PLC0415

        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            reason="Correção sintética",
        )
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=2,
            request=request,
        )
        release_version(clinic_id=graph.clinic_a, version_id=amendment.pk)
        second = export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
        manifest, files = _package_parts(second.data)
        assert manifest["record_count"] == 2
        states = [entry["state"] for entry in manifest["files"]]
        assert states == ["superseded", "finalized"]
        lineage = [
            json.loads(files[entry["path"]])["amendment_of_version"]
            for entry in manifest["files"]
        ]
        assert lineage == [None, 1]
        assert verify_export_package(second.data).ok


def test_export_denials_and_tamper_detection(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        patient_id = version.document.encounter.patient_id
        # No releases yet: the package is empty but still verifiable.
        empty = export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
        manifest, files = _package_parts(empty.data)
        assert manifest["record_count"] == 0
        assert files == {}
        assert verify_export_package(empty.data).ok
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        package = export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
    # Non-physicians and foreign clinics cannot export.
    for actor, org in (
        (graph.shared_user, graph.organization_a),
        (graph.clinic_admin, graph.organization_a),
        (graph.shared_user, graph.organization_b),
    ):
        with (
            runtime_role(),
            tenant_context(actor, org),
            pytest.raises(RetentionAccessDeniedError),
        ):
            export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
    # A physician without a care relationship is denied.
    other = create_role_actor(graph, UserClinicRole.Role.PHYSICIAN)
    with (
        runtime_role(),
        tenant_context(other.pk, graph.organization_a),
        pytest.raises(RetentionAccessDeniedError),
    ):
        export_staff_records(clinic_id=graph.clinic_a, patient_id=patient_id)
    # Tampered packages fail verification closed.
    manifest, files = _package_parts(package.data)
    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w") as archive:
        altered = dict(manifest)
        altered["files"] = [dict(manifest["files"][0], sha256="0" * 64)]
        archive.writestr("manifest.json", json.dumps(altered))
        for name, data in files.items():
            archive.writestr(name, data)
    verdict = verify_export_package(tampered.getvalue())
    assert verdict.ok is False
    assert verdict.reason_code in {"digest_mismatch", "manifest_digest_mismatch"}
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stored = verify_stored_export(
            clinic_id=graph.clinic_a,
            export_id=package.export.pk,
            data=tampered.getvalue(),
        )
        assert stored.ok is False
        # A valid package under a forged export id is non-enumerating.
        with pytest.raises(RetentionAccessDeniedError):
            verify_stored_export(
                clinic_id=graph.clinic_a,
                export_id=uuid4(),
                data=package.data,
            )
        # The original documents are untouched by every export and verify.
        stored_version = ClinicalDocumentVersion.objects.get(pk=version.pk)
        assert stored_version.state == "finalized"
        assert stored_version.content_digest == version.content_digest


def test_patient_export_requires_records_session(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        patient_id = version.document.encounter.patient_id
    with setup_context(graph.organization_a):
        from apps.intake.models import (  # noqa: PLC0415
            PatientClinicEnrollment,
        )

        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=patient_id, clinic_id=graph.clinic_a
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    with runtime_role():
        session_id = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session_id is not None
    with runtime_role(), patient_session_context(session_id) as binding:
        assert binding is not None
        assert "records" in binding.operations
        records = patient_released_records()
        assert [record.version_id for record in records] == [version.pk]
        package = export_patient_records()
        assert package.export.kind == "patient"
        assert package.export.patient_session_id == session_id
        manifest, files = _package_parts(package.data)
        assert manifest["record_count"] == 1
        assert manifest["files"][0]["record_id"] == str(version.pk)
        assert (
            sha256(files[manifest["files"][0]["path"]]).hexdigest()
            == (manifest["files"][0]["sha256"])
        )
        assert verify_export_package(package.data).ok
        # The receipt is readable by the session and immutable.
        assert RecordExport.objects.filter(pk=package.export.pk).exists()
        with pytest.raises(DatabaseError), transaction.atomic():
            RecordExport.objects.filter(pk=package.export.pk).delete()
    _assert_patient_views(graph, version.pk, expected=2, session_id=str(session_id))
    # A session without the records operation is denied.
    with setup_context(graph.organization_a):
        PatientSession.objects.filter(pk=session_id).update(
            operations=["enrollment_view"]
        )
    with runtime_role(), patient_session_context(session_id):
        with pytest.raises(RetentionAccessDeniedError):
            patient_released_records()
        with pytest.raises(RetentionAccessDeniedError):
            export_patient_records()
    # No session at all fails closed.
    with runtime_role(), pytest.raises(RetentionAccessDeniedError):
        export_patient_records()


def test_retention_tables_rls_acl_and_immutability(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    manager = admin(graph)
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        hold = place_hold(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
            authority="Autoridade",
            reason="Motivo",
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname,relrowsecurity,relforcerowsecurity,"
            "relowner::regrole::text FROM pg_class WHERE relname = ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in TABLES
        }
        cursor.execute(
            "SELECT tablename,policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=ANY(%s)",
            [list(TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("retention_retentionpolicy", "setup_tenant"),
            ("retention_retentionpolicy", "policy_read"),
            ("retention_retentionpolicy", "policy_insert"),
            ("retention_retentionpolicy", "policy_update"),
            ("retention_legalhold", "setup_tenant"),
            ("retention_legalhold", "hold_read"),
            ("retention_legalhold", "hold_insert"),
            ("retention_legalhold", "hold_release"),
            ("retention_recordrelease", "setup_tenant"),
            ("retention_recordrelease", "release_read"),
            ("retention_recordrelease", "release_insert"),
            ("retention_recordrelease", "release_revoke"),
            ("retention_recordexport", "setup_tenant"),
            ("retention_recordexport", "export_read"),
            ("retention_recordexport", "export_insert_staff"),
            ("retention_recordexport", "export_insert_patient"),
        }
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        # Identity columns are immutable; deletes are forbidden everywhere.
        with pytest.raises(DatabaseError), transaction.atomic():
            RecordRelease.objects.filter(pk=release.pk).update(patient_id=uuid4())
        with pytest.raises(DatabaseError), transaction.atomic():
            RecordRelease.objects.filter(pk=release.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            LegalHold.objects.filter(pk=hold.pk).update(record_id=uuid4())
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_b):
        assert not RecordRelease.objects.exists()
        assert not LegalHold.objects.exists()
        assert not RetentionPolicy.objects.exists()


def test_http_workspace_actions_and_denials(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    manager = admin(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    url = f"/retention/clinics/{graph.clinic_a}/"
    with physician_client(graph) as client:
        assert client.get(url).status_code == 200
        assert "no-store" in client.get(url).headers["Cache-Control"]
        # Physicians see the status but hold no manager controls.
        html = client.get(url).content.decode()
        assert 'id="policy-form"' not in html
        assert 'id="hold-form"' not in html
        assert client.post(url, {"action": "invalid"}).status_code == 403
        assert (
            client.post(
                url,
                {
                    "action": "propose_policy",
                    "record_class": "ehr.encounter",
                    "retention_days": "30",
                },
            ).status_code
            == 403
        )
    with staff_client(manager.pk) as client:
        assert client.get(url).status_code == 200
        # Propose, approve and retire through the real middleware path.
        assert (
            client.post(
                url,
                {
                    "action": "propose_policy",
                    "record_class": "ehr.document_version",
                    "retention_days": "0",
                },
            ).status_code
            == 302
        )
        page = client.get(url)
        policy = page.context["policies"][0]
        assert policy.state == "proposed"
        assert (
            client.post(
                url, {"action": "approve_policy", "policy_id": policy.pk}
            ).status_code
            == 302
        )
        with tenant_context(manager.pk, graph.organization_a):
            policy.refresh_from_db()
        assert policy.state == "approved"
        # A held record's disposal check renders the denial surface.
        assert (
            client.post(
                url,
                {
                    "action": "place_hold",
                    "record_class": "ehr.document_version",
                    "record_id": version.pk,
                    "authority": "Autoridade sintética",
                    "reason": "Motivo sintético",
                },
            ).status_code
            == 302
        )
        denied = client.post(
            url,
            {
                "action": "check_disposal",
                "record_class": "ehr.document_version",
                "record_id": version.pk,
            },
        )
        assert denied.status_code == 403
        assert "Descarte negado" in denied.content.decode()
        with tenant_context(graph.physician, graph.organization_a):
            assert ClinicalDocumentVersion.objects.filter(pk=version.pk).exists()
        with tenant_context(manager.pk, graph.organization_a):
            hold = LegalHold.objects.get(record_id=version.pk)
        # Releasing the hold makes the record eligible again.
        assert (
            client.post(
                url,
                {
                    "action": "release_hold",
                    "hold_id": hold.pk,
                    "release_authority": "Autoridade de liberação",
                    "release_reason": "Motivo de liberação",
                },
            ).status_code
            == 302
        )
        eligible = client.post(
            url,
            {
                "action": "check_disposal",
                "record_class": "ehr.document_version",
                "record_id": version.pk,
            },
        )
        assert eligible.status_code == 200
        assert "Elegível" in eligible.content.decode()
    # Receptionists read status but hold no write controls.
    receptionist = create_role_actor(graph, UserClinicRole.Role.RECEPTIONIST)
    with staff_client(receptionist.pk) as client:
        page = client.get(url)
        assert page.status_code == 200
        html = page.content.decode()
        assert 'id="policy-form"' not in html
        assert 'id="hold-form"' not in html
        assert (
            client.post(
                url,
                {
                    "action": "place_hold",
                    "record_class": "ehr.document_version",
                    "record_id": version.pk,
                    "authority": "x",
                    "reason": "y",
                },
            ).status_code
            == 403
        )
    # A foreign clinic's workspace is non-enumerating.
    foreign = f"/retention/clinics/{graph.clinic_b}/"
    with physician_client(graph) as client:
        assert client.get(foreign).status_code == 403
        assert (
            client.post(
                foreign,
                {"action": "export", "patient_id": uuid4()},
            ).status_code
            == 403
        )


def test_http_release_export_and_patient_download(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    url = f"/retention/clinics/{graph.clinic_a}/"
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        patient_id = version.document.encounter.patient_id
    with physician_client(graph) as client:
        # Release through the staff workspace, then export the package.
        assert (
            client.post(
                url, {"action": "release", "version_id": version.pk}
            ).status_code
            == 302
        )
        download = client.post(url, {"action": "export", "patient_id": patient_id})
        assert download.status_code == 200
        assert download.headers["Content-Type"] == "application/zip"
        assert "no-store" in download.headers["Cache-Control"]
        assert download.headers["X-Content-Type-Options"] == "nosniff"
        manifest, _files = _package_parts(download.content)
        assert manifest["record_count"] == 1
        assert verify_export_package(download.content).ok
        # A draft version can never be released over HTTP either.
        with tenant_context(graph.physician, graph.organization_a):
            draft = amend_document(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                reason="Correção sintética",
            )
        conflict = client.post(url, {"action": "release", "version_id": draft.pk})
        assert conflict.status_code == 409
        # Unauthorized export attempts are denied.
        assert (
            client.post(url, {"action": "export", "patient_id": uuid4()}).status_code
            == 403
        )
    # The patient downloads the same package through their session.
    with setup_context(graph.organization_a):
        from apps.intake.models import (  # noqa: PLC0415
            PatientClinicEnrollment,
        )

        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=patient_id, clinic_id=graph.clinic_a
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    patient = Client()
    with runtime_role():
        redeemed = patient.post(
            f"/patient/access/{graph.clinic_a}/", {"code": invitation.secret}
        )
        assert redeemed.status_code == 302
        page = patient.get("/patient/records/")
        assert page.status_code == 200
        assert "Relato sintético" in page.content.decode()
        download = patient.post("/patient/records/", {"action": "export"})
        assert download.status_code == 200
        assert download.headers["Content-Type"] == "application/zip"
        manifest, _files = _package_parts(download.content)
        assert manifest["kind"] == "patient"
        assert manifest["record_count"] == 1
        assert verify_export_package(download.content).ok
        # A forged session id and a staff-only path both fail closed.
        forged = Client()
        forged_session = forged.session
        forged_session[PATIENT_SESSION_KEY] = str(uuid4())
        forged_session.save()
        assert forged.get("/patient/records/").status_code == 403
        assert forged.post("/patient/records/", {"action": "export"}).status_code == 403
        anonymous = Client()
        assert anonymous.get("/patient/records/").status_code == 403
    _assert_patient_views(
        graph,
        version.pk,
        expected=2,
        session_id=str(patient.session[PATIENT_SESSION_KEY]),
    )


def _assert_patient_views(
    graph: RbacGraph, version_id: UUID, *, expected: int, session_id: str
) -> None:
    """Assert the patient reads appended session-bound viewed events."""
    with setup_context(graph.organization_a):
        viewed = AuditEvent.objects.filter(
            event_type="ehr.record.viewed",
            affected_record_id=str(version_id),
            actor_user_id=session_id,
        )
        assert viewed.count() == expected
        assert {row.payload["object_verb"] for row in viewed} == {"viewed"}
        from apps.audit.services import verify_chain  # noqa: PLC0415

        verify_chain(graph.organization_a)


def test_http_staff_export_audits_each_version(rbac_graph: RbacGraph) -> None:
    """A staff export appends one viewed event per delivered version."""
    graph = rbac_graph
    appointment, template = seed(graph)
    url = f"/retention/clinics/{graph.clinic_a}/"
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        patient_id = version.document.encounter.patient_id
        # An amended, re-released lineage exports two versions.
        from apps.ehr.services import record_clinical_note  # noqa: PLC0415

        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            reason="Correção sintética",
        )
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=2,
            request=request,
        )
        release_version(clinic_id=graph.clinic_a, version_id=amendment.pk)
    with setup_context(graph.organization_a):
        baseline = (
            AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first()
            or 0
        )
    with physician_client(graph) as client:
        download = client.post(url, {"action": "export", "patient_id": patient_id})
        assert download.status_code == 200
        assert download.headers["Content-Type"] == "application/zip"
        manifest, _files = _package_parts(download.content)
        assert manifest["record_count"] == 2
        assert verify_export_package(download.content).ok
    with setup_context(graph.organization_a):
        delta = list(
            AuditEvent.objects.filter(seq__gt=baseline)
            .order_by("seq")
            .values_list(
                "seq",
                "event_type",
                "affected_record_type",
                "affected_record_id",
                "actor_user_id",
            )
        )
        viewed = [row for row in delta if row[1] == "ehr.record.viewed"]
        # Each exported version is audited once, under the staff actor.
        assert sorted(str(row[3]) for row in viewed) == sorted(
            [str(version.pk), str(amendment.pk)]
        )
        assert {row[2] for row in viewed} == {"ehr.document_version"}
        assert {row[4] for row in viewed} == {graph.physician}
        # The export receipt is still appended after the clinical reads.
        created = [row for row in delta if row[1] == "retention.export.created"]
        assert len(created) == 1
        assert created[0][4] == graph.physician
        assert max(row[0] for row in viewed) < created[0][0]
        from apps.audit.services import verify_chain  # noqa: PLC0415

        verify_chain(graph.organization_a)


def test_apply_retention_policy_stays_unimplemented() -> None:
    """The deferred purge entrypoint fails closed until a pipeline exists."""
    from apps.retention.services import apply_retention_policy  # noqa: PLC0415

    with pytest.raises(NotImplementedError, match="Phase"):
        apply_retention_policy()


def test_policy_and_hold_validation_errors(rbac_graph: RbacGraph) -> None:
    """Invalid inputs are form-level errors; nothing is written."""
    graph = rbac_graph
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        with pytest.raises(ValidationError):
            propose_policy(
                clinic_id=graph.clinic_a,
                record_class="ehr.unknown",
                retention_days=30,
            )
        with pytest.raises(ValidationError):
            propose_policy(
                clinic_id=graph.clinic_a,
                record_class="ehr.encounter",
                retention_days=-1,
            )
        with pytest.raises(ValidationError):
            place_hold(
                clinic_id=graph.clinic_a,
                record_class="ehr.encounter",
                record_id=uuid4(),
                authority="  ",
                reason="Motivo",
            )
        with pytest.raises(ValidationError):
            release_hold(
                clinic_id=graph.clinic_a,
                hold_id=uuid4(),
                authority="",
                reason="Motivo",
            )
        # Unknown clinics and policies are non-enumerating denials.
        with pytest.raises(RetentionAccessDeniedError):
            propose_policy(
                clinic_id=uuid4(),
                record_class="ehr.encounter",
                retention_days=30,
            )
        with pytest.raises(RetentionAccessDeniedError):
            approve_policy(clinic_id=graph.clinic_a, policy_id=uuid4())
        with pytest.raises(RetentionAccessDeniedError):
            retire_policy(clinic_id=graph.clinic_a, policy_id=uuid4())
        # Approving an approved policy is a fixed conflict.
        policy = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.encounter",
            retention_days=30,
        )
        approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        with pytest.raises(RetentionConflictError, match="precondition_failed"):
            approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        # Retiring twice returns the retired row unchanged.
        retired = retire_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        again = retire_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        assert again.retired_at == retired.retired_at
        # A hold on a foreign-clinic record is denied.
        with pytest.raises(RetentionAccessDeniedError):
            place_hold(
                clinic_id=graph.clinic_b,
                record_class="ehr.encounter",
                record_id=uuid4(),
                authority="Autoridade",
                reason="Motivo",
            )


def test_disposal_not_elapsed_and_verify_edges(rbac_graph: RbacGraph) -> None:
    """A future eligibility date and malformed packages fail closed."""
    graph = rbac_graph
    manager = admin(graph)
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        policy = propose_policy(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            retention_days=36500,
        )
        approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        decision = record_disposition(
            clinic_id=graph.clinic_a,
            record_class="ehr.document_version",
            record_id=version.pk,
        )
        with pytest.raises(RetentionAccessDeniedError):
            request_disposal(
                clinic_id=graph.clinic_a,
                record_class="ehr.document_version",
                record_id=version.pk,
            )
    assert decision.allowed is False
    assert decision.reason_code == "retention_not_elapsed"
    assert decision.eligible_at is not None
    # Malformed packages fail verification closed without touching the database.
    assert verify_export_package(b"not a zip").reason_code == "malformed_package"
    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w"):
        pass
    assert verify_export_package(empty_zip.getvalue()).reason_code == (
        "manifest_missing"
    )
    bad_manifest = io.BytesIO()
    with zipfile.ZipFile(bad_manifest, "w") as archive:
        archive.writestr("manifest.json", b'{"files": "no"}')
    assert verify_export_package(bad_manifest.getvalue()).reason_code == (
        "manifest_malformed"
    )
    bad_entry = io.BytesIO()
    with zipfile.ZipFile(bad_entry, "w") as archive:
        archive.writestr("manifest.json", b'{"files": [{"path": 1}]}')
    assert verify_export_package(bad_entry.getvalue()).reason_code == (
        "manifest_malformed"
    )
    no_claim = io.BytesIO()
    with zipfile.ZipFile(no_claim, "w") as archive:
        archive.writestr("manifest.json", b'{"files": []}')
    assert verify_export_package(no_claim.getvalue()).reason_code == (
        "manifest_malformed"
    )
    extra_file = io.BytesIO()
    with zipfile.ZipFile(extra_file, "w") as archive:
        archive.writestr("manifest.json", b'{"files": [], "manifest_sha256": "x"}')
        archive.writestr("records/extra.json", b"{}")
    assert verify_export_package(extra_file.getvalue()).reason_code == (
        "file_set_mismatch"
    )


def test_http_form_errors_and_revoke(rbac_graph: RbacGraph) -> None:
    """Invalid forms render 400; revoke and retire work through the workspace."""
    graph = rbac_graph
    appointment, template = seed(graph)
    manager = admin(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release = release_version(clinic_id=graph.clinic_a, version_id=version.pk)
    url = f"/retention/clinics/{graph.clinic_a}/"
    with staff_client(manager.pk) as client:
        # Invalid form payloads render the workspace with 400, not a write.
        assert (
            client.post(
                url,
                {"action": "propose_policy", "record_class": "ehr.unknown"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                url,
                {
                    "action": "place_hold",
                    "record_class": "ehr.encounter",
                    "record_id": "not-a-uuid",
                    "authority": "x",
                    "reason": "y",
                },
            ).status_code
            == 400
        )
        assert (
            client.post(
                url,
                {
                    "action": "release_hold",
                    "hold_id": "not-a-uuid",
                    "release_authority": "x",
                    "release_reason": "y",
                },
            ).status_code
            == 400
        )
        assert (
            client.post(url, {"action": "release", "version_id": "bad"}).status_code
            == 400
        )
        assert (
            client.post(
                url, {"action": "revoke_release", "release_id": "bad"}
            ).status_code
            == 400
        )
        assert (
            client.post(url, {"action": "export", "patient_id": "bad"}).status_code
            == 400
        )
        assert (
            client.post(
                url,
                {
                    "action": "check_disposal",
                    "record_class": "ehr.encounter",
                    "record_id": "bad",
                },
            ).status_code
            == 400
        )
        # A hold placed then released through the workspace stays recorded.
        assert (
            client.post(
                url,
                {
                    "action": "place_hold",
                    "record_class": "ehr.document_version",
                    "record_id": version.pk,
                    "authority": "Autoridade sintética",
                    "reason": "Motivo sintético",
                },
            ).status_code
            == 302
        )
        with tenant_context(manager.pk, graph.organization_a):
            hold = LegalHold.objects.get(record_id=version.pk)
        assert (
            client.post(
                url,
                {
                    "action": "release_hold",
                    "hold_id": hold.pk,
                    "release_authority": "Autoridade de liberação",
                    "release_reason": "Motivo de liberação",
                },
            ).status_code
            == 302
        )
        with tenant_context(manager.pk, graph.organization_a):
            hold.refresh_from_db()
        assert hold.released_at is not None
        # A policy proposed then retired through the workspace stays recorded.
        assert (
            client.post(
                url,
                {
                    "action": "propose_policy",
                    "record_class": "ehr.encounter",
                    "retention_days": "30",
                },
            ).status_code
            == 302
        )
        with tenant_context(manager.pk, graph.organization_a):
            policy = RetentionPolicy.objects.get(record_class="ehr.encounter")
        assert (
            client.post(
                url, {"action": "retire_policy", "policy_id": policy.pk}
            ).status_code
            == 302
        )
        with tenant_context(manager.pk, graph.organization_a):
            policy.refresh_from_db()
        assert policy.state == "retired"
    # The physician revokes the release through the workspace.
    with physician_client(graph) as client:
        assert (
            client.post(
                url, {"action": "revoke_release", "release_id": release.pk}
            ).status_code
            == 302
        )
        with tenant_context(graph.physician, graph.organization_a):
            release.refresh_from_db()
        assert release.revoked_at is not None


def test_patient_records_gate_and_wrong_action(rbac_graph: RbacGraph) -> None:
    """The patient surface denies unknown actions and missing sessions."""
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        patient_id = version.document.encounter.patient_id
    with setup_context(graph.organization_a):
        from apps.intake.models import (  # noqa: PLC0415
            PatientClinicEnrollment,
        )

        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=patient_id, clinic_id=graph.clinic_a
        )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment.pk
        )
    patient = Client()
    with runtime_role():
        assert (
            patient.post(
                f"/patient/access/{graph.clinic_a}/", {"code": invitation.secret}
            ).status_code
            == 302
        )
        # A non-export action is denied; the records page still lists content.
        assert (
            patient.post("/patient/records/", {"action": "delete"}).status_code == 403
        )
        assert patient.get("/patient/records/").status_code == 200


def test_resolvers_enforce_tenant_and_role_authority(rbac_graph: RbacGraph) -> None:
    """Runtime-role resolver calls never leak foreign clinical metadata."""
    graph = rbac_graph
    manager = admin(graph)
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = finalized(graph, appointment, template, request)
        release_version(clinic_id=graph.clinic_a, version_id=version.pk)
        patient_id = version.document.encounter.patient_id
    with (
        runtime_role(),
        tenant_context(manager.pk, graph.organization_a),
        connection.cursor() as cursor,
    ):
        # The manager's own tenant resolves the record scope and author.
        cursor.execute(
            "SELECT * FROM clinic_app.retention_record_scope(%s, %s)",
            ["ehr.document_version", str(version.pk)],
        )
        scope = cursor.fetchone()
        assert scope is not None
        assert scope[0] == graph.clinic_a
        assert scope[1] == graph.organization_a
        cursor.execute(
            "SELECT clinic_app.retention_author_label(%s)",
            [str(graph.physician)],
        )
        label = cursor.fetchone()
        assert label is not None
        assert label[0]
        cursor.execute(
            "SELECT * FROM clinic_app.retention_care_patients(%s)",
            [str(graph.clinic_a)],
        )
        assert cursor.fetchall() == []
    # A foreign tenant's principal gets zero rows and no label, exactly like
    # an unknown record: no clinic, organization or staff metadata escapes.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_b),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.retention_record_scope(%s, %s)",
            ["ehr.document_version", str(version.pk)],
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT clinic_app.retention_author_label(%s)",
            [str(graph.physician)],
        )
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT * FROM clinic_app.retention_care_patients(%s)",
            [str(graph.clinic_a)],
        )
        assert cursor.fetchall() == []
    # Same-tenant non-managers cannot resolve record scope either; the staff
    # author label stays available because staff may see colleague names.
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT * FROM clinic_app.retention_record_scope(%s, %s)",
            ["ehr.document_version", str(version.pk)],
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT clinic_app.retention_author_label(%s)",
            [str(graph.physician)],
        )
        label = cursor.fetchone()
        assert label is not None
        assert label[0]
    # The patient export still resolves its own author label end to end.
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        records = _released_for_patient_staff(graph.clinic_a, patient_id)
        assert [record.version_id for record in records] == [version.pk]
        assert records[0].author_label

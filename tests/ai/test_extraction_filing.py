"""Attachment identity and independent resolver refusals on real PostgreSQL."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
import pytest
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.extraction import (
    candidate_data,
    collect_extraction,
    extraction_source,
    review_extraction,
    start_extraction,
)
from apps.ai.extraction_contracts import ExtractionInputError, filing_payload
from apps.core.integration import execute_operation
from apps.core.secrets import secret_store
from apps.ehr.attachments import AttachmentInput, scan_attachment, upload_attachment
from apps.ehr.models import Encounter
from apps.intake.models import PatientClinicEnrollment
from apps.orders.models import ResultMatch, ResultObservation, ResultVersion
from apps.orders.services import (
    IdentityEvidence,
    OrdersAccessDeniedError,
    ResultInput,
    acknowledge_result,
    ingest_result,
)
from apps.tenancy.envelope import protect
from django.db import DatabaseError, connection, transaction
from psycopg import sql

from ai.support import DEFAULT_BUDGET
from ai.test_extraction import pdf_fixture
from ai.test_extraction_runtime import extracted_job
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path
    from uuid import UUID

    from apps.ai.models import ExtractionCandidateRecord, ExtractionJob
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def matching_submission(job: ExtractionJob) -> ResultInput:
    """Use independently stored patient identity, not an attachment assertion."""
    with owner_context(job.organization_id):
        match = ResultMatch.objects.get(version_id=job.result_version_id)
        enrollment = PatientClinicEnrollment.objects.select_related("patient").get(
            pk=job.patient_enrollment_id
        )
        return ResultInput(
            order_id=match.order_id,
            identity=IdentityEvidence(
                name=enrollment.patient.full_name,
                birth_date=enrollment.patient.birth_date,
            ),
            content_type="application/pdf",
            data=pdf_fixture(rotation=90),
            event_id=uuid4(),
        )


def raw_confirmation(row: ExtractionCandidateRecord, target: UUID) -> tuple[UUID, bool]:
    """Bypass Python checks, but send the real encrypted confirmation envelope."""
    payload = filing_payload(candidate_data(row), confirmer=str(row.job.user_id))
    payload["extraction_candidate"] = str(row.pk)
    source = protect(
        purpose="orders.observation.source",
        plaintext=json.dumps(payload, separators=(",", ":")).encode(),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.ai_review_extraction(%s,%s,'confirmed',%s,%s)",
            [row.pk, target, source, secret_store().get_secret("tenant-kek")],
        )
        receipt = cursor.fetchone()
    assert receipt is not None
    return cast("tuple[UUID, bool]", receipt)


@contextmanager
def no_candidate_writes(superuser_database_url: str) -> Iterator[None]:
    """Detect attempted filing before another guard can mask a missing precheck.

    This adds a test-only refusal, never disables a production guard, and is
    removed before the catalog tests. A deleted resolver precheck reaches this
    canary and raises P0001 instead of the required early refusal SQLSTATE.
    """
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        owner.execute(
            "CREATE FUNCTION clinic_app.test_extraction_no_write() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'test write probe' USING ERRCODE='P0001'; END $$"
        )
        try:
            owner.execute(
                "CREATE TRIGGER aa_test_extraction_no_write BEFORE UPDATE ON "
                "clinic_app.ai_extractioncandidate FOR EACH ROW "
                "EXECUTE FUNCTION clinic_app.test_extraction_no_write()"
            )
            yield
        finally:
            owner.execute(
                "DROP TRIGGER IF EXISTS aa_test_extraction_no_write "
                "ON clinic_app.ai_extractioncandidate"
            )
            owner.execute("DROP FUNCTION clinic_app.test_extraction_no_write()")


@contextmanager
def review_clock(superuser_database_url: str, reference: datetime) -> Iterator[None]:
    """Control the resolver's real clock site without aging authority or policies."""
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        definition = owner.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.ai_review_extraction(uuid,uuid,text,bytea,text)'::regprocedure)"
        ).fetchone()
        assert definition is not None
        assert "statement_timestamp()" in definition[0]
        controlled = definition[0].replace(
            "statement_timestamp()", sql.Literal(reference).as_string(owner)
        )
        owner.execute(controlled)
        try:
            yield
        finally:
            owner.execute(definition[0])


def test_attachment_requires_scan_and_independent_exact_byte_identity_match(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    settings: SettingsWrapper,
    tmp_path: Path,
    superuser_database_url: str,
) -> None:
    settings.EHR_ATTACHMENT_ROOT = tmp_path / "synthetic-extraction-objects"
    source_job = extracted_job(rbac_graph, monkeypatch)
    assert source_job.result_version_id is not None
    submission = matching_submission(source_job)
    with owner_context(rbac_graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            pk=source_job.patient_enrollment_id
        )
        encounter = Encounter.objects.get(
            patient_id=enrollment.patient_id,
            clinic_id=source_job.clinic_id,
            physician_id=rbac_graph.physician,
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        attachment = upload_attachment(
            clinic_id=source_job.clinic_id,
            encounter_id=encounter.pk,
            upload=AttachmentInput(
                file_name="synthetic-extraction.pdf",
                declared_type="application/pdf",
                data=submission.data,
            ),
        )
        with pytest.raises(GatewayAccessDeniedError):
            extraction_source(
                clinic_id=source_job.clinic_id, attachment_id=attachment.pk
            )
        with connection.cursor() as cursor:
            query = "SELECT clinic_app.ai_extraction_source_allowed(%s,%s,NULL,%s,%s)"
            arguments: list[UUID | str] = [
                source_job.clinic_id,
                source_job.patient_enrollment_id,
                attachment.pk,
                attachment.sha256,
            ]
            cursor.execute(query, arguments)
            assert cursor.fetchone() == (False,)
        scan_attachment(clinic_id=source_job.clinic_id, attachment_id=attachment.pk)
        resolved = extraction_source(
            clinic_id=source_job.clinic_id,
            attachment_id=attachment.pk,
            include_bytes=True,
        )
        assert resolved.data == submission.data
        assert resolved.enrollment_id == source_job.patient_enrollment_id
        with connection.cursor() as cursor:
            cursor.execute(query, arguments)
            assert cursor.fetchone() == (True,)
        job = start_extraction(
            clinic_id=source_job.clinic_id,
            attachment_id=attachment.pk,
            budget=DEFAULT_BUDGET,
        )
        operation = job.invocation.operation_id
        wrong = ingest_result(
            clinic_id=job.clinic_id,
            submission=replace(
                submission,
                identity=IdentityEvidence(
                    name="Synthetic wrong patient",
                    birth_date=submission.identity.birth_date,
                ),
            ),
        )
        different = ingest_result(
            clinic_id=job.clinic_id,
            submission=replace(submission, data=pdf_fixture(), event_id=uuid4()),
        )
    assert operation is not None
    assert (wrong.state, different.state) == ("quarantined", "matched")
    with runtime_role():
        assert execute_operation(operation_id=operation) == "succeeded"
    with as_actor(rbac_graph, rbac_graph.physician):
        collect_extraction(clinic_id=job.clinic_id, job_id=job.pk)
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.select_related("job").get(position=0)
        with pytest.raises(ExtractionInputError):
            review_extraction(
                clinic_id=job.clinic_id, candidate_id=row.pk, decision="confirmed"
            )
        with pytest.raises(OrdersAccessDeniedError):
            review_extraction(
                clinic_id=job.clinic_id,
                candidate_id=row.pk,
                decision="confirmed",
                result_version_id=wrong.version_id,
            )
    with (
        no_candidate_writes(superuser_database_url),
        as_actor(rbac_graph, rbac_graph.physician),
    ):
        for target, state in (
            (wrong.version_id, "23514"),
            (different.version_id, "42501"),
        ):
            with pytest.raises(DatabaseError) as refused, transaction.atomic():
                raw_confirmation(row, target)
            assert getattr(refused.value.__cause__, "sqlstate", None) == state
    with as_actor(rbac_graph, rbac_graph.physician):
        identifier = review_extraction(
            clinic_id=job.clinic_id,
            candidate_id=row.pk,
            decision="confirmed",
            result_version_id=source_job.result_version_id,
        )
        assert identifier is not None
        observation = ResultObservation.objects.get(pk=identifier)
        assert observation.version_id == source_job.result_version_id
        assert (
            observation.source["provenance"],
            observation.source["confirmer"],
        ) == ("imported", str(rbac_graph.physician))
        assert (job.result_version_id, job.attachment_id) == (None, attachment.pk)


def test_resolver_rejects_another_current_version_even_with_identical_bytes(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    submission = matching_submission(job)
    with as_actor(rbac_graph, rbac_graph.physician):
        other = ingest_result(clinic_id=job.clinic_id, submission=submission)
        row = job.candidates.select_related("job").get(position=0)
        assert job.result_version_id is not None
        assert other.version_id != job.result_version_id
        assert other.state == "matched"
        assert ResultVersion.objects.get(pk=other.version_id).sha256 == job.sha256
        with transaction.atomic():
            assert raw_confirmation(row, job.result_version_id)[1] is True
            transaction.set_rollback(True)
    with (
        no_candidate_writes(superuser_database_url),
        as_actor(rbac_graph, rbac_graph.physician),
    ):
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            raw_confirmation(row, other.version_id)
        assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"


def test_resolver_expiry_uses_database_time_at_the_exact_deadline(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    assert job.result_version_id is not None
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.select_related("job").get(position=0)
        with transaction.atomic():
            assert raw_confirmation(row, job.result_version_id)[1] is True
            transaction.set_rollback(True)
    with (
        review_clock(superuser_database_url, row.expires_at),
        as_actor(rbac_graph, rbac_graph.physician),
    ):
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            raw_confirmation(row, job.result_version_id)
        assert getattr(refused.value.__cause__, "sqlstate", None) == "40001"


def test_resolver_refuses_filing_before_writing_into_an_acknowledged_version(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    assert job.result_version_id is not None
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.select_related("job").get(position=0)
        with transaction.atomic():
            assert raw_confirmation(row, job.result_version_id)[1] is True
            transaction.set_rollback(True)
        acknowledge_result(
            clinic_id=job.clinic_id,
            version_id=job.result_version_id,
            disposition="no_action",
        )
    with (
        no_candidate_writes(superuser_database_url),
        as_actor(rbac_graph, rbac_graph.physician),
    ):
        with pytest.raises(DatabaseError) as refused, transaction.atomic():
            raw_confirmation(row, job.result_version_id)
        assert getattr(refused.value.__cause__, "sqlstate", None) == "23514"
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()

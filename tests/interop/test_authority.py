"""Import authority: exact roles, identical denials, step-up and DB guards."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import User, UserClinicRole
from apps.interop import services, tasks
from apps.interop.models import (
    ImportBatch,
    ImportedRecord,
    ImportFile,
    ImportJob,
    ImportTransition,
)
from apps.tenancy.envelope import _kek
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from interop.support import (
    SYNTHETIC_PDF,
    approve,
    archive,
    committed,
    history_csv,
    history_row,
    import_world,
    operating,
    patients_csv,
    run_batches,
    to_dry_run,
    upload,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from interop.support import ImportWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
IMPORT_ROLES = frozenset({"clinic_manager", "clinic_admin", "org_admin", "owner"})
IDENTITY_ROLES = frozenset({"clinic_manager", "clinic_admin"})
# Each payload column the import authority must never read, as the exact query.
PAYLOAD_READS = (
    "SELECT content FROM clinic_app.interop_importedrecord WHERE job_id=%s",
    "SELECT source_record_id FROM clinic_app.interop_importedrecord WHERE job_id=%s",
    "SELECT original_author FROM clinic_app.interop_importedrecord WHERE job_id=%s",
    "SELECT original_recorded_at FROM clinic_app.interop_importedrecord"
    " WHERE job_id=%s",
    "SELECT data FROM clinic_app.interop_importfile"
    " WHERE job_id=%s AND role='attachment'",
    "SELECT data FROM clinic_app.interop_importfile WHERE job_id=%s AND role='source'",
)


@pytest.mark.parametrize("role", UserClinicRole.Role.values)
def test_import_authority_follows_the_staff_management_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor = create_role_actor(rbac_graph, UserClinicRole.Role(role)).pk
    source = patients_csv(2)
    world = import_world(rbac_graph)
    with operating(world, actor):
        if role not in IMPORT_ROLES:
            with pytest.raises(services.ImportAccessDeniedError):
                services.upload_import(
                    clinic_id=rbac_graph.clinic_a,
                    source_kind="csv",
                    data=source,
                    declared_sha256=hashlib.sha256(source).hexdigest(),
                    idempotency_key=uuid4(),
                )
            with pytest.raises(services.ImportAccessDeniedError):
                services.import_jobs(clinic_id=rbac_graph.clinic_a)
            return
        job = services.upload_import(
            clinic_id=rbac_graph.clinic_a,
            source_kind="csv",
            data=source,
            declared_sha256=hashlib.sha256(source).hexdigest(),
            idempotency_key=uuid4(),
        )
        profiled = services.profile_import(
            clinic_id=rbac_graph.clinic_a, job_id=job.job_id, expected_sequence=1
        )
        assert profiled.job.state == "profiled"
    report = to_dry_run(world, source)
    with operating(world, actor):
        if role not in IDENTITY_ROLES:
            # Comparing with existing patients needs demographics access.
            with pytest.raises(services.ImportAccessDeniedError):
                services.dry_run_report(
                    clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
                )
        else:
            assert (
                services.dry_run_report(
                    clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
                ).digest
                == report.digest
            )
        signed = services.approve_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=report.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=8,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=False,
                duplicates_acknowledged=False,
            ),
        )
        assert signed.state == "approved"


def test_unknown_and_foreign_jobs_share_one_denial(rbac_graph: RbacGraph) -> None:
    world = import_world(rbac_graph)
    job = upload(world, patients_csv(1))
    foreign = User.objects.create(username=f"sintetico-interop-b-{uuid4().hex}")
    with owner_context(rbac_graph.organization_b):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_b,
            clinic_id=rbac_graph.clinic_c,
            user=foreign,
            role=UserClinicRole.Role.CLINIC_MANAGER,
        )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    with runtime_role(), tenant_context(foreign.pk, rbac_graph.organization_b):
        errors = []
        for job_id in (job.job_id, uuid4()):
            with pytest.raises(services.ImportAccessDeniedError) as denied:
                services.import_job(clinic_id=rbac_graph.clinic_c, job_id=job_id)
            errors.append(str(denied.value))
        with pytest.raises(services.ImportAccessDeniedError):
            services.import_job(clinic_id=rbac_graph.clinic_a, job_id=job.job_id)
        assert services.import_jobs(clinic_id=rbac_graph.clinic_c) == ()
    assert errors == ["import access denied", "import access denied"]


def test_commit_and_rollback_require_this_actors_fresh_step_up(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    other = import_world(rbac_graph)
    report = to_dry_run(world, patients_csv(2))
    approved = approve(world, report)
    for key in ("", "sintetico-sem-sessao", other.session):
        with operating(world), pytest.raises(services.ImportStepUpRequiredError):
            services.commit_import(
                clinic_id=rbac_graph.clinic_a,
                job_id=report.job.job_id,
                expected_sequence=approved.sequence,
                session_key=key,
            )
    # The guard refuses a batch inserted without the session binding.
    with operating(world):
        job = ImportJob.objects.get(pk=report.job.job_id)
        with transaction.atomic(), pytest.raises(DatabaseError) as raw:
            ImportBatch.objects.create(
                organization_id=job.organization_id,
                clinic_id=job.clinic_id,
                job=job,
                sequence=1,
                dataset="patients",
                first_row=1,
                last_row=2,
                rows_sha256="0" * 64,
                requested_by_id=world.operator,
            )
    assert getattr(raw.value.__cause__, "sqlstate", None) == "42501"
    with operating(world):
        receipt = services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
        replay = services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    assert replay.batch_ids == receipt.batch_ids
    with operating(world), pytest.raises(services.ImportStepUpRequiredError):
        services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            reason="abandoned",
            session_key=other.session,
        )


def test_org_admin_prepares_and_signs_off_but_cannot_commit_identity(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = import_world(rbac_graph, "org_admin")
    manager = import_world(rbac_graph)
    report = to_dry_run(manager, patients_csv(3))
    approved = approve(admin, report)
    with operating(admin), pytest.raises(services.ImportAccessDeniedError):
        services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=admin.session,
        )
    with operating(manager):
        receipt = services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=manager.session,
        )
    # The queued batch runs under its requester, the clinic manager.
    monkeypatch.setattr(tasks, "fair_acquire", lambda **_kwargs: True)
    with runtime_role():
        assert tasks.commit_batch(batch_id=str(receipt.batch_ids[0])) == "committed"
    with operating(admin):
        rolled = services.rollback_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence + 1,
            reason="cutover_cancelled",
            session_key=admin.session,
        )
    assert rolled.hidden == 3


def test_revoked_import_authority_stops_queued_batches(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    report = to_dry_run(world, patients_csv(2))
    approved = approve(world, report)
    with operating(world):
        receipt = services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(user_id=world.operator).update(
            revoked_at=timezone.now()
        )
    monkeypatch.setattr(tasks, "fair_acquire", lambda **_kwargs: True)
    with runtime_role():
        assert tasks.commit_batch(batch_id=str(receipt.batch_ids[0])) == "denied"
        assert tasks.commit_batch(batch_id=str(uuid4())) == "missing"
    with owner_context(rbac_graph.organization_a):
        assert ImportBatch.objects.get(pk=receipt.batch_ids[0]).state == "pending"


def test_bulk_fairness_defers_without_side_effects(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    report = to_dry_run(world, patients_csv(2))
    approved = approve(world, report)
    with operating(world):
        receipt = services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    sent: list[dict[str, object]] = []
    queues: list[str] = []

    def fair(*, organization_id: object, queue: str) -> bool:
        queues.append(queue)
        assert organization_id == rbac_graph.organization_a
        return False

    monkeypatch.setattr(tasks, "fair_acquire", fair)
    monkeypatch.setattr(
        tasks.commit_batch_job, "apply_async", lambda **kwargs: sent.append(kwargs)
    )
    with runtime_role():
        assert tasks.commit_batch(batch_id=str(receipt.batch_ids[0])) == "deferred"
    assert queues == ["bulk"]
    assert sent == [
        {"kwargs": {"batch_id": str(receipt.batch_ids[0])}, "countdown": 30}
    ]
    with owner_context(rbac_graph.organization_a):
        assert ImportBatch.objects.get(pk=receipt.batch_ids[0]).state == "pending"


def _raw_transition(world: ImportWorld, job_id: UUID, sequence: int, state: str) -> str:
    with operating(world):
        job = ImportJob.objects.get(pk=job_id)
        try:
            with transaction.atomic():
                ImportTransition.objects.create(
                    organization_id=job.organization_id,
                    clinic_id=job.clinic_id,
                    job=job,
                    sequence=sequence,
                    state=state,
                    actor_id=world.operator,
                    report={"synthetic": True},
                    digest="1" * 64,
                    rollback_window_hours=1 if state == "approved" else None,
                )
        except DatabaseError as error:
            diag = getattr(error.__cause__, "diag", None)
            return str(getattr(diag, "message_primary", ""))
    return "accepted"


def test_database_refuses_illegal_edges_stale_sequences_and_history_edits(
    rbac_graph: RbacGraph, superuser_database_url: str
) -> None:
    world = import_world(rbac_graph)
    job = upload(world, patients_csv(1))
    assert _raw_transition(world, job.job_id, 2, "approved") == "interop_transition"
    assert _raw_transition(world, job.job_id, 5, "profiled") == "interop_stale"
    assert _raw_transition(world, job.job_id, 1, "uploaded") == "interop_stale"
    with operating(world), pytest.raises(services.ImportConflictError) as early:
        services.dry_run_import(
            clinic_id=rbac_graph.clinic_a, job_id=job.job_id, expected_sequence=1
        )
    assert early.value.code == "interop_transition"
    with operating(world), pytest.raises(services.ImportConflictError) as stale:
        services.profile_import(
            clinic_id=rbac_graph.clinic_a, job_id=job.job_id, expected_sequence=7
        )
    assert stale.value.code == "interop_stale"
    with psycopg.connect(superuser_database_url) as raw:
        for statement in (
            "UPDATE clinic_app.interop_importtransition SET state='approved'",
            "DELETE FROM clinic_app.interop_importjob",
            "TRUNCATE clinic_app.interop_importfile CASCADE",
        ):
            with pytest.raises(psycopg.errors.CheckViolation) as refused:
                raw.execute(statement)
            raw.rollback()
            assert refused.value.diag.message_primary == "interop_immutable"
    with operating(world), connection.cursor() as cursor:
        with pytest.raises(DatabaseError) as denied, transaction.atomic():
            cursor.execute("UPDATE clinic_app.interop_importjob SET size_bytes=1")
        assert getattr(denied.value.__cause__, "sqlstate", None) == "42501"


def test_rollback_window_is_enforced_by_the_database(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = import_world(rbac_graph)
    report, _receipt = committed(world, patients_csv(2), monkeypatch)
    with psycopg.connect(superuser_database_url) as raw:
        raw.execute(
            "ALTER TABLE clinic_app.interop_importtransition DISABLE TRIGGER USER"
        )
        raw.execute(
            "UPDATE clinic_app.interop_importtransition"
            " SET created_at=created_at-interval '100 hours'"
            " WHERE job_id=%s AND state='committed'",
            (report.job.job_id,),
        )
        raw.execute(
            "ALTER TABLE clinic_app.interop_importtransition ENABLE TRIGGER USER"
        )
    with operating(world):
        state = services.import_job(
            clinic_id=rbac_graph.clinic_a, job_id=report.job.job_id
        )
        with pytest.raises(services.ImportConflictError) as late:
            services.rollback_import(
                clinic_id=rbac_graph.clinic_a,
                job_id=report.job.job_id,
                expected_sequence=state.sequence,
                reason="data_quality",
                session_key=world.session,
            )
    assert late.value.code == "interop_rollback_window"


def _refused(statement: str, params: list[UUID]) -> str | None:
    with (
        pytest.raises(DatabaseError) as refused,
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(statement, params)
    return getattr(refused.value.__cause__, "sqlstate", None)


def test_import_authority_reads_metadata_but_never_clinical_payloads(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    members = {
        "patients.csv": patients_csv(1),
        "history.csv": history_csv(
            [
                history_row("EV-1", "SIN-000001", "Evolucao", text="Nota sintetica"),
                history_row("AX-1", "SIN-000001", "Anexo", file="laudo.pdf"),
            ]
        ),
        "files/laudo.pdf": SYNTHETIC_PDF,
    }
    report, _receipt = committed(world, archive(members), monkeypatch, kind="zip")
    job_id = report.job.job_id
    with operating(world):
        # Every payload column is refused to the importer's real runtime session,
        # also through the ORM's default row read.
        assert {
            statement: _refused(statement, [job_id]) for statement in PAYLOAD_READS
        } == dict.fromkeys(PAYLOAD_READS, "42501")
        with pytest.raises(DatabaseError), transaction.atomic():
            ImportedRecord.objects.filter(job_id=job_id).first()
        with pytest.raises(DatabaseError), transaction.atomic():
            ImportFile.objects.filter(job_id=job_id, role="source").first()
        # Metadata and checksums stay readable for the import's own bookkeeping.
        records = dict(
            ImportedRecord.objects.filter(job_id=job_id).values_list("kind", "pk")
        )
        assert set(records) == {"patient", "note", "attachment"}
        enrollment = (
            ImportedRecord.objects.filter(job_id=job_id, kind="patient")
            .values_list("enrollment_id", flat=True)
            .get()
        )
        assert set(
            ImportFile.objects.filter(job_id=job_id).values_list("role", "size_bytes")
        ) == {("source", report.job.size_bytes), ("attachment", len(SYNTHETIC_PDF))}
        # The resolvers give the importer nothing clinical and no stored source.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.interop_history(%s, %s)",
                [rbac_graph.clinic_a, enrollment],
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT count(*) FROM clinic_app.interop_attachment(%s, %s)",
                [rbac_graph.clinic_a, records["attachment"]],
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT files, total_bytes, mismatches"
                " FROM clinic_app.interop_attachment_checks(%s, %s)",
                [job_id, _kek()],
            )
            assert cursor.fetchone() == (1, len(SYNTHETIC_PDF), 0)
        assert _refused("SELECT clinic_app.interop_source(%s)", [job_id]) == "42501"


def test_stored_source_reaches_only_the_pipeline_of_an_in_flight_job(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = import_world(rbac_graph)
    org_admin = import_world(rbac_graph, "org_admin")
    reception = import_world(rbac_graph, "receptionist")
    data = patients_csv(2)
    report = to_dry_run(world, data)
    statement = "SELECT octet_length(clinic_app.interop_source(%s)) > 0"

    def reads(actor: ImportWorld) -> bool | str | None:
        with operating(world, actor.operator):
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(statement, [report.job.job_id])
                    row = cursor.fetchone()
            except DatabaseError as error:
                return getattr(error.__cause__, "sqlstate", None)
        return None if row is None else bool(row[0])

    # Before approval an import authority (also an org admin) may re-parse it.
    assert reads(world) is True
    assert reads(org_admin) is True
    approved = approve(world, report)
    # Once approved, only an actor who may also see identities (the commit).
    assert reads(world) is True
    assert reads(org_admin) == "42501"
    with operating(world):
        services.commit_import(
            clinic_id=rbac_graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    run_batches(world, report.job.job_id, monkeypatch)
    # Committed: nobody reads the stored source through the database any more.
    assert reads(world) == "42501"
    assert reads(org_admin) == "42501"
    # Without import authority it was never readable.
    with operating(world, reception.operator):
        assert _refused(statement, [report.job.job_id]) == "42501"

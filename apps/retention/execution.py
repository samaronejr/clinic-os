"""Synthetic-only retention scheduling and machine-attributed execution."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from celery import current_app
from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode
from django.conf import settings
from django.db import connections, transaction

from apps.audit.services import record_phase1_event
from apps.core.telemetry import METRICS
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.retention.purge import PurgePlan, execute_purge
from apps.retention.storage import SyntheticPurgeStorage
from apps.scribe.storage import media_location
from apps.tenancy.db import service_principal_context

if TYPE_CHECKING:
    from uuid import UUID

TRANSIENT_CLASSES = ("raw_audio", "ai_artifact", "transcript_partial")


class RetentionExecutionDeniedError(Exception):
    """Unknown, foreign-clinic and unauthorized principals deny identically."""


@dataclass(frozen=True, slots=True)
class RetentionRun:
    """Metadata-only scheduling result; dry runs never publish destructive work."""

    dry_run: bool
    due: int
    blocked_hold: int
    purge_ids: tuple[UUID, ...]


def schedule_purges(*, clinic_id: UUID, record_class: str) -> RetentionRun:
    """Use the caller's existing machine context; live terms await EG-2/EG-3."""
    if (
        resolved_data_mode() != SYNTHETIC_DATA_MODE
        or "agent" not in connections
        or not connections["agent"].in_atomic_block
        or record_class not in TRANSIENT_CLASSES
    ):
        raise RetentionExecutionDeniedError
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.retention_schedule(%s,%s)",
            [clinic_id, record_class],
        )
        rows = cursor.fetchall()
    if not rows:
        raise RetentionExecutionDeniedError
    dry_run, due, held, principal_id = rows[0][:4]
    ids = tuple(row[4] for row in rows if row[4] is not None)
    for purge_id in ids:
        state = next(row[5] for row in rows if row[4] == purge_id)
        if state:
            record_phase1_event(
                "retention.purge." + state,
                clinic_id=clinic_id,
                affected_record_id=purge_id,
            )
        transaction.on_commit(
            partial(
                publish_purge,
                clinic_id=clinic_id,
                principal_id=principal_id,
                purge_id=purge_id,
            ),
            using="agent",
        )
    return RetentionRun(bool(dry_run), int(due), int(held), ids)


def publish_purge(*, clinic_id: UUID, principal_id: UUID, purge_id: UUID) -> None:
    """Publish opaque references only; the retained job survives broker failure."""
    current_app.send_task(
        "apps.retention.tasks.purge_artifact",
        kwargs={
            "clinic_id": str(clinic_id),
            "principal_id": str(principal_id),
            "purge_id": str(purge_id),
        },
        queue="bulk",
    )


@dataclass(frozen=True, slots=True)
class DatabasePurgeBackend:
    """No plaintext reads or borrowed staff GUCs on the dedicated agent alias."""

    clinic_id: UUID
    principal_id: UUID

    def prepare(self, purge_id: UUID) -> PurgePlan | None:
        """Commit the tombstone and key destruction before returning object keys."""
        with service_principal_context(
            principal_id=self.principal_id, clinic_id=self.clinic_id
        ):
            with connections["agent"].cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clinic_app.retention_prepare(%s,%s)",
                    [self.clinic_id, purge_id],
                )
                row = cursor.fetchone()
            if row is None:
                return None
            record_phase1_event(
                "retention.purge." + row[2],
                clinic_id=self.clinic_id,
                affected_record_id=purge_id,
            )
            if row[2] != "purging":
                return None
        if row[0] and (set(row[3]) != {media_location()} or settings.SCRIBE_DEV_MINIO):
            raise AttachmentStorageError
        return PurgePlan(
            purge_id=purge_id,
            object_keys=tuple(row[0]),
            backup_copy_expires_at=row[1],
        )

    def confirm(self, purge_id: UUID, key: str) -> None:
        """Append one absence confirmation under the same retained target scope."""
        with (
            service_principal_context(
                principal_id=self.principal_id, clinic_id=self.clinic_id
            ),
            connections["agent"].cursor() as cursor,
        ):
            cursor.execute(
                "SELECT clinic_app.retention_confirm(%s,%s,%s)",
                [self.clinic_id, purge_id, key],
            )

    def complete(self, purge_id: UUID) -> None:
        """Verify complete target membership before a final receipt."""
        with service_principal_context(
            principal_id=self.principal_id, clinic_id=self.clinic_id
        ):
            with connections["agent"].cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.retention_complete(%s,%s)",
                    [self.clinic_id, purge_id],
                )
            record_phase1_event(
                "retention.purge.purged",
                clinic_id=self.clinic_id,
                affected_record_id=purge_id,
            )

    def fail(self, purge_id: UUID, reason: str) -> None:
        """Expose failure with a fixed reason, durable task and PHI-free metric."""
        with service_principal_context(
            principal_id=self.principal_id, clinic_id=self.clinic_id
        ):
            with connections["agent"].cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.retention_failure(%s,%s,%s)",
                    [self.clinic_id, purge_id, reason],
                )
            record_phase1_event(
                "retention.purge.failure",
                clinic_id=self.clinic_id,
                affected_record_id=purge_id,
            )
        METRICS.record_retention_failure()


def run_purge(*, clinic_id: UUID, principal_id: UUID, purge_id: UUID) -> None:
    """Fence pending uploads, then execute with no open transaction during I/O."""
    if resolved_data_mode() != SYNTHETIC_DATA_MODE:
        raise RetentionExecutionDeniedError
    backend = DatabasePurgeBackend(clinic_id, principal_id)
    # Same boundary as the capture outbox. The dedicated machine connection
    # holds the session lock across its short transactions and external delete.
    agent = connections["agent"]
    key = f"clinic-lock-v1:comms-subject:scribe-consent:{clinic_id}"
    with agent.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", [key])
    try:
        storage = (
            None
            if settings.SCRIBE_DEV_MINIO
            else SyntheticPurgeStorage(Path(settings.SCRIBE_MEDIA_ROOT))
        )
        execute_purge(purge_id=purge_id, backend=backend, storage=storage)
    except AttachmentStorageError:
        backend.fail(purge_id, "storage_unavailable")
    finally:
        with agent.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", [key])

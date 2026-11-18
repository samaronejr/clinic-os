"""Approved governed imports for the clinical fairness workload."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ops.perf.broker import QUEUE_PREFIX, import_queue
from ops.perf.children import ChildContext, child_setup, context_for
from ops.perf.rig import attach
from ops.perf.runtime import CLIENT_HOST, runtime_role
from ops.perf.staff_http import provision_login, sign_in

if TYPE_CHECKING:
    from multiprocessing.synchronize import Event

    from ops.perf.dataset import Tenant

IMPORT_ROWS = 10_000
DEFER_BACKOFF_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ImportContext:
    """Opaque references to the approved batches and their expected row counts."""

    scope: ChildContext
    batches: tuple[tuple[str, int], ...]
    queue_name: str


def prepare_import(tenant: Tenant) -> ImportContext:
    """Upload, validate, approve and queue a real synthetic migration."""
    from apps.identity.models import User  # noqa: PLC0415
    from apps.interop import services  # noqa: PLC0415
    from apps.interop.models import ImportBatch  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.test import Client  # noqa: PLC0415

    login = provision_login(User.objects.get(pk=tenant.admin_id))
    client = Client(HTTP_HOST=CLIENT_HOST)
    with runtime_role():
        sign_in(client, login)
        session = client.session.session_key
    if not session:
        message = "import operator has no verified session"
        raise RuntimeError(message)
    source = io.StringIO(newline="")
    writer = csv.writer(source, lineterminator="\n")
    writer.writerow(["source_id", "legal_name"])
    run = uuid4().hex[:12]
    for number in range(IMPORT_ROWS):
        writer.writerow([f"perf-{run}-{number}", f"Importado Sintetico {run} {number}"])
    body = source.getvalue().encode("utf-8")
    mapping: dict[str, object] = {
        "schema": "import-mapping-v1",
        "source_kind": "csv",
        "date_format": "YYYY-MM-DD",
        "datetime_format": "YYYY-MM-DDTHH:MM",
        "csv": {"patients": {"delimiter": ",", "encoding": "utf-8"}},
        "datasets": {
            "patients": {
                "columns": {"source_id": "source_id", "legal_name": "legal_name"},
                "values": {},
            }
        },
    }
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        uploaded = services.upload_import(
            clinic_id=tenant.clinic_id,
            source_kind="csv",
            data=body,
            declared_sha256=hashlib.sha256(body).hexdigest(),
            idempotency_key=uuid4(),
        )
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        profile = services.profile_import(
            clinic_id=tenant.clinic_id,
            job_id=uploaded.job_id,
            expected_sequence=uploaded.sequence,
        )
        saved = services.save_mapping(
            clinic_id=tenant.clinic_id, code=f"perf-{run}", document=mapping
        )
        mapped = services.apply_mapping(
            clinic_id=tenant.clinic_id,
            job_id=uploaded.job_id,
            mapping_id=saved.mapping_id,
            expected_sequence=profile.job.sequence,
        )
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        dry_run = services.dry_run_import(
            clinic_id=tenant.clinic_id,
            job_id=uploaded.job_id,
            expected_sequence=mapped.sequence,
        )
    if dry_run.job.valid_count != IMPORT_ROWS or dry_run.job.rejected_count:
        message = "synthetic import source was not fully accepted"
        raise RuntimeError(message)
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        approved = services.approve_import(
            clinic_id=tenant.clinic_id,
            job_id=uploaded.job_id,
            expected_sequence=dry_run.job.sequence,
            plan=services.CutoverPlan(
                rollback_window_hours=24,
                source_frozen=True,
                backup_verified=True,
                rejects_acknowledged=True,
                duplicates_acknowledged=True,
            ),
        )
    queue_name = QUEUE_PREFIX + uuid4().hex
    with (
        import_queue(queue_name),
        runtime_role(),
        tenant_context(tenant.admin_id, tenant.organization_id),
    ):
        receipt = services.commit_import(
            clinic_id=tenant.clinic_id,
            job_id=uploaded.job_id,
            expected_sequence=approved.sequence,
            session_key=session,
        )
        batches = tuple(
            (str(batch.pk), batch.last_row - batch.first_row + 1)
            for batch in ImportBatch.objects.filter(pk__in=receipt.batch_ids).order_by(
                "sequence"
            )
        )
    return ImportContext(context_for(tenant), batches, queue_name)


def import_child(
    context: ImportContext,
    _index: int,
    stop: Event,
    warm: Event,
    counts: Any,  # noqa: ANN401 - multiprocessing shared array
    failed: Event,
) -> None:
    """Invoke the bulk task entrypoint with its real transaction and quota check."""
    stopped = False
    try:
        attach("worker")
        child_setup(context.scope)
        from apps.interop.tasks import commit_batch  # noqa: PLC0415

        with import_queue(context.queue_name), runtime_role():
            for batch_id, rows in context.batches:
                while not stop.is_set():
                    outcome = commit_batch(batch_id=batch_id)
                    if outcome == "deferred":
                        with counts.get_lock():
                            counts[1] += 1
                        stop.wait(DEFER_BACKOFF_SECONDS)
                        continue
                    if outcome != "committed":
                        message = "governed import batch did not commit"
                        raise RuntimeError(message)
                    with counts.get_lock():
                        counts[0] += rows
                    warm.set()
                    break
                if stop.is_set():
                    stopped = True
                    return
        message = "governed import exhausted before the clinical trial ended"
        raise RuntimeError(message)
    finally:
        if not stopped:
            failed.set()
            warm.set()

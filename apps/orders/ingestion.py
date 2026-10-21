"""Authenticate scope, retain bytes, and request exact matching without review."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import asdict
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from django.db import DatabaseError, connection, transaction

from apps.audit.services import record_phase1_event
from apps.core.secrets import secret_store
from apps.orders.access import lock_series, require_ingest
from apps.orders.models import Result, ResultObservation, ResultVersion
from apps.orders.storage import store_source, validate_source
from apps.orders.values import (
    IngestReceipt,
    OrdersAccessDeniedError,
    OrdersConflictError,
    ResultInput,
    validate_submission,
)

if TYPE_CHECKING:
    from uuid import UUID


def _receipt(*, clinic_id: UUID, event_id: UUID) -> tuple[IngestReceipt, str] | None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.orders_event_receipt(%s,%s)",
                [clinic_id, event_id],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        if getattr(error.__cause__, "sqlstate", None) == "42501":
            raise OrdersAccessDeniedError from error
        raise
    if row is None:
        return None
    return IngestReceipt(row[0], row[1], row[2], row[3]), row[4]


def verify_match(*, clinic_id: UUID, version_id: UUID, order_id: UUID | None) -> str:
    """Only the resolver can file a source; direct match inserts are not granted."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.orders_verify_identity(%s,%s,%s,%s)",
            [
                clinic_id,
                version_id,
                order_id,
                secret_store().get_secret("tenant-kek"),
            ],
        )
        row = cursor.fetchone()
    if row is None:
        raise OrdersAccessDeniedError
    if row[1]:
        event = "matched" if row[0] == "matched" else "quarantined"
        record_phase1_event(
            f"orders.result.{event}", clinic_id=clinic_id, affected_record_id=version_id
        )
        if row[2] is not None:
            record_phase1_event(
                "orders.task.created", clinic_id=clinic_id, affected_record_id=row[2]
            )
    return cast("str", row[0])


@transaction.atomic
def ingest_result(*, clinic_id: UUID, submission: ResultInput) -> IngestReceipt:
    """Return the identical event receipt; deny altered and late events."""
    uploader = require_ingest(clinic_id=clinic_id, order_id=submission.order_id)
    validate_submission(submission)
    validate_source(submission)
    fingerprint = submission.fingerprint()
    lock_series(identifier=submission.event_id)
    replay = _receipt(clinic_id=clinic_id, event_id=submission.event_id)
    if replay is not None:
        if replay[1] != fingerprint:
            raise OrdersConflictError(code="event_mismatch")
        return replay[0]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.orders_ingest_scope(%s,%s,%s,%s)",
            [
                clinic_id,
                submission.order_id,
                submission.result_id,
                submission.predecessor_id,
            ],
        )
        scope = cursor.fetchone()
    if scope is None:
        raise OrdersConflictError(code="source_superseded")
    organization_id, lab_account_id, number = scope
    result_id = submission.result_id or uuid4()
    lock_series(identifier=result_id)
    # Resolve again after taking the shared correction/review fence.
    if submission.result_id is not None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.orders_is_current(%s)", [submission.predecessor_id]
            )
            if cursor.fetchone() != (True,):
                raise OrdersConflictError(code="source_superseded")
    else:
        Result.objects.create(
            id=result_id, organization_id=organization_id, clinic_id=clinic_id
        )
    key = secrets.token_hex(32)
    store_source(key=key, organization_id=organization_id, data=submission.data)
    version = ResultVersion.objects.create(
        organization_id=organization_id,
        clinic_id=clinic_id,
        result_id=result_id,
        number=number,
        predecessor_id=submission.predecessor_id,
        event_id=submission.event_id,
        fingerprint=fingerprint,
        proposed_order_id=submission.order_id,
        identity=submission.identity.payload(),
        storage_key=key,
        sha256=hashlib.sha256(submission.data).hexdigest(),
        size_bytes=len(submission.data),
        content_type=submission.content_type,
        critical=submission.critical
        or any(row.abnormal_flag == "critical" for row in submission.observations),
        uploader_id=uploader,
        lab_account_id=lab_account_id,
    )
    for position, observation in enumerate(submission.observations):
        ResultObservation.objects.create(
            organization_id=organization_id,
            clinic_id=clinic_id,
            version=version,
            position=position,
            source=asdict(observation),
        )
    record_phase1_event(
        "orders.result.ingested", clinic_id=clinic_id, affected_record_id=version.pk
    )
    state = verify_match(
        clinic_id=clinic_id, version_id=version.pk, order_id=submission.order_id
    )
    return IngestReceipt(result_id, version.pk, number, state)

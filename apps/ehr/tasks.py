"""After-commit projection wake-up and broker-independent durable recovery scan."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from uuid import UUID

from celery import shared_task
from celery.exceptions import OperationalError
from django.db import connection, transaction

if TYPE_CHECKING:
    from django.db.models import Model


def refresh_timeline(*, refresh_id: str | None = None) -> int:
    """Run the fixed metadata-only worker function without a staff GUC."""
    from ops.release.activation import require_live_runtime  # noqa: PLC0415

    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        if refresh_id is None:
            cursor.execute("SELECT * FROM clinic_app.ehr_timeline_due()")
            identifiers = tuple(row[0] for row in cursor.fetchall())
        else:
            identifiers = (UUID(refresh_id),)
        completed = 0
        for identifier in identifiers:
            cursor.execute("SELECT clinic_app.ehr_timeline_refresh(%s)", [identifier])
            completed += cursor.fetchone() == (True,)
        return completed


refresh_timeline_job = shared_task(name="apps.ehr.tasks.refresh_timeline")(
    refresh_timeline
)


def _dispatch() -> None:
    try:
        refresh_timeline_job.apply_async(retry=False)
    except OperationalError:
        # The committed database queue is authoritative; Beat recovers broker loss.
        return


def wake_projection(sender: type[Model], **_kwargs: object) -> None:
    """No source values enter the broker; rolled-back changes publish nothing."""
    del sender
    transaction.on_commit(_dispatch)

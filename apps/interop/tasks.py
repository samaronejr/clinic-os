"""Organization export jobs on the fair ``bulk`` queue; references only in messages."""

from __future__ import annotations

import os
from typing import Protocol
from uuid import UUID

from celery import shared_task
from django.db import connection
from ops.release.activation import require_live_runtime

from apps.core.fairness import acquire_or_defer
from apps.interop.export_builder import build_organization_export


class _TaskSender(Protocol):
    """Publish only opaque references through the registered Celery task."""

    def apply_async(self, *, kwargs: dict[str, str], queue: str) -> object: ...


class _Request(Protocol):
    """The bound request the fair-queue deferral re-publishes."""

    args: tuple[object, ...]
    kwargs: dict[str, object]


class _BoundTask(Protocol):
    """A bound Celery task as ``acquire_or_defer`` uses it."""

    @property
    def request(self) -> _Request: ...

    def apply_async(self, **options: object) -> object: ...


def dispatch_export(
    *, export_id: UUID, organization_id: UUID, requested_by_id: UUID
) -> None:
    """Hand one committed export request to the bulk worker."""
    require_live_runtime(os.environ)
    build_export.apply_async(
        kwargs={
            "export_id": str(export_id),
            "organization_id": str(organization_id),
            "requested_by_id": str(requested_by_id),
        },
        queue="bulk",
    )


def run_build(
    task: _BoundTask, *, export_id: str, organization_id: str, requested_by_id: str
) -> str:
    """Build after this organization's fair-queue admission; defer otherwise."""
    organization = UUID(organization_id)
    if not acquire_or_defer(task, organization_id=organization, queue="bulk"):
        return "deferred"
    return build_organization_export(
        export_id=UUID(export_id),
        organization_id=organization,
        requested_by_id=UUID(requested_by_id),
    )


def dispatch_due() -> int:
    """Re-dispatch requested exports whose after-commit handoff never arrived."""
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.interop_due_exports()")
        due = cursor.fetchall()
    for export_id, organization_id, requested_by_id in due:
        dispatch_export(
            export_id=export_id,
            organization_id=organization_id,
            requested_by_id=requested_by_id,
        )
    return len(due)


def expire() -> int:
    """Expire past-due archives and fail exports whose requester lost authority."""
    require_live_runtime(os.environ)
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.interop_expire_exports()")
        row = cursor.fetchone()
    return 0 if row is None else int(row[0])


build_export: _TaskSender = shared_task(
    name="apps.interop.tasks.build_export",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)(run_build)
dispatch_due_exports = shared_task(name="apps.interop.tasks.dispatch_due_exports")(
    dispatch_due
)
expire_exports = shared_task(name="apps.interop.tasks.expire_exports")(expire)

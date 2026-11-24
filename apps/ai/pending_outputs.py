"""Data access for materialized model output, including organization exports.

The default/base managers and the privileged export projection apply the same
PostgreSQL predicate before pagination or serialization. Ordinary row access
is still subject to the table's original FORCE RLS and audience restrictions.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import connection, models

from apps.ai.contracts import GatewayAccessDeniedError
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.brief_contracts import ChartDisplay
    from apps.ai.contracts import Purpose


class PendingOutput:
    """A concrete subclass may project unaccepted AI output; FORCE RLS decides."""


class OutputProvenance:
    """A classified lineage edge holding human decisions or operational state."""


class SealedOutput:
    """Previously serialized pending rows: release only after rechecking origins."""


@cache
def relations() -> frozenset[str]:
    """Derive export dependencies from models, never UI routes."""
    return frozenset(
        model._meta.db_table  # noqa: SLF001 - Django's public metadata contract
        for model in apps.get_models()
        if issubclass(model, PendingOutput)
    )


def rows[M: models.Model](model: type[M]) -> models.QuerySet[M]:
    """Return the caller's RLS-governed rows; never switch database or role."""
    if not issubclass(model, (PendingOutput, SealedOutput)):
        msg = "not a pending-output model"
        raise TypeError(msg)
    manager = cast("models.Manager[M]", model._default_manager)  # noqa: SLF001
    return manager.all()


def reselect[M: models.Model](instance: M) -> M:
    """Reauthorize a retained instance immediately before serializing its fields."""
    visible = rows(type(instance)).filter(pk=instance.pk).first()
    if visible is None:
        raise GatewayAccessDeniedError
    return visible


def export_page(
    *, export_id: UUID, relation: str, after: str | None, size: int
) -> list[tuple[str, str | dict[str, object]]]:
    """Fetch one filtered page after the resolver authenticates its owner."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT row_key,row_data FROM clinic_app.interop_export_rows(%s,%s,%s,%s)",
            [export_id, relation, after, size],
        )
        return cast("list[tuple[str, str | dict[str, object]]]", cursor.fetchall())


def archive_current(export_id: UUID) -> bool:
    """Never release a byte of an old part if a pending source is now withheld."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_archive_current(%s)", [export_id])
        row = cursor.fetchone()
        return row is not None and row[0] is True


def record_export_origin(
    origins: list[tuple[str, str]], relation: str, row: dict[str, object]
) -> None:
    """Keep only immutable keys inside the build receipt, not in archive files."""
    if relation in relations():
        origins.append((relation, str(row["id"])))


def withheld_artifact(
    clinic: UUID,
    key: UUID,
    purpose: Purpose | None = None,
    display: ChartDisplay | None = None,
) -> bool:
    """Distinguish a refused original clinical turn, never identify a foreign one."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_pending_refusal(%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                clinic,
                key,
                purpose.value if purpose else None,
                display.enrollment if display else None,
                display.encounter if display else None,
                display.appointment if display else None,
                display.session if display else None,
                secret_store().get_secret(KEK_SECRET_NAME) if display else None,
            ],
        )
        row = cursor.fetchone()
        return row is not None and row[0] is True

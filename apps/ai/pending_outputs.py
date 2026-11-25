"""Data access for materialized model output, including organization exports.

The default/base managers and the privileged export projection apply the same
PostgreSQL predicate before pagination or serialization. Ordinary row access
is still subject to the table's original FORCE RLS and audience restrictions.

A query result is not a permit to render. Presenters, views, partials,
exports and downloads keep only ``PendingKey`` selectors from earlier reads;
``ai_pending_visible`` re-selects every requested row in one statement inside
the response transaction and passes ``VisiblePending`` handles to a callback
that renders synchronously. A withheld row is simply absent, and a handle
kept past its callback re-checks, rather than reusing, its row.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from threading import Event, get_ident
from typing import TYPE_CHECKING, cast, final

from django.apps import apps
from django.db import connection, connections, models, transaction
from django.db.models.functions import Cast
from django.http.response import HttpResponseBase
from django.template.response import SimpleTemplateResponse

from apps.ai.contracts import GatewayAccessDeniedError
from apps.consent.decision_fence import try_hold_decision, try_hold_organization_read
from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from uuid import UUID

    from apps.ai.brief_contracts import ChartDisplay
    from apps.ai.contracts import Purpose


class PendingOutput:
    """A concrete subclass may project unaccepted AI output; FORCE RLS decides."""


class OutputProvenance:
    """A classified lineage edge holding human decisions or operational state."""


class SealedOutput:
    """Previously serialized pending rows: release only after rechecking origins."""


@dataclass(frozen=True, slots=True)
class PendingKey[M: models.Model]:
    """An untrusted selector. It carries no row, payload, actor or permission."""

    model: type[M]
    pk: UUID


def key[M: models.Model](row: M) -> PendingKey[M]:
    """Discard all previously loaded fields and select_related caches."""
    return PendingKey(type(row), row.pk)


_ISSUER = object()


class _VisibleOutput:
    """Marker for projection permits; never instantiate these at a caller."""

    __slots__ = ()


@final
class VisiblePending[M: models.Model](_VisibleOutput):
    """A typed, non-transferable projection handle, not a cached authorization."""

    __slots__ = ("_clinic", "_key")

    def __init__(
        self, selector: PendingKey[M], clinic: UUID | None, issuer: object
    ) -> None:
        """Accept only an issuance made in this module's scoped decision."""
        if issuer is not _ISSUER:
            raise GatewayAccessDeniedError
        self._key = selector
        self._clinic = clinic

    @property
    def record(self) -> M:
        """Only the issuing, currently projecting frame may materialize fields."""
        frame = _CURRENT.get()
        if frame is None or frame.get(self._key) is not self:
            raise GatewayAccessDeniedError
        return cast("M", frame._records[self._key])  # noqa: SLF001 - same boundary

    def project[R](self, build: Callable[[VisiblePending[M]], R]) -> R | None:
        """Revalidate a callback even when invoked from another live frame."""

        def single(batch: RenderSelection) -> R | None:
            fresh = batch.get(self._key)
            return None if fresh is None else build(fresh)

        return ai_pending_visible((self._key,), single, clinic_id=self._clinic)


@dataclass(frozen=True, slots=True)
class ExportPage:
    """Untrusted selectors for the separately authorized organization export."""

    export_id: UUID
    relation: str
    after: str | None
    size: int


@final
class VisibleExportRow(_VisibleOutput):
    """A consent-filtered row for the current export page, not for staff views."""

    __slots__ = ("_index",)

    def __init__(self, index: int, issuer: object) -> None:
        """Bind to the issuing frame, which alone holds the exported fields."""
        if issuer is not _ISSUER:
            raise GatewayAccessDeniedError
        self._index = index

    @property
    def record(self) -> tuple[str, str | dict[str, object]]:
        """Expose the record only during synchronous projection of its page."""
        frame = _CURRENT.get()
        if frame is None or not frame.owns_export(self, self._index):
            raise GatewayAccessDeniedError
        return frame._export_data[self._index]  # noqa: SLF001 - same boundary


@dataclass(frozen=True, slots=True)
class RenderSelection(_VisibleOutput):
    """The output of one SQL snapshot. Callers cannot issue handles from it."""

    _records: dict[PendingKey[models.Model], models.Model]
    _handles: dict[PendingKey[models.Model], VisiblePending[models.Model]]
    _export_data: list[tuple[str, str | dict[str, object]]]
    _export_rows: tuple[VisibleExportRow, ...]
    _issuer: object
    _thread: int
    _ended: Event

    def __post_init__(self) -> None:
        """Reject construction outside the issuing boundary."""
        if self._issuer is not _ISSUER:
            raise GatewayAccessDeniedError

    def get[M: models.Model](self, selector: PendingKey[M]) -> VisiblePending[M] | None:
        """Return the checked handle or omit a missing or withheld selector."""
        if not self._active():
            raise GatewayAccessDeniedError
        return cast("VisiblePending[M] | None", self._handles.get(selector))

    def many[M: models.Model](
        self, selectors: Iterable[PendingKey[M]]
    ) -> tuple[VisiblePending[M], ...]:
        """Preserve caller order, omitting only selectors withheld by RLS."""
        return tuple(row for k in selectors if (row := self.get(k)) is not None)

    def export_rows(self) -> tuple[VisibleExportRow, ...]:
        """Return the filtered page in the export resolver's cursor order."""
        if not self._active():
            raise GatewayAccessDeniedError
        return self._export_rows

    def owns_export(self, row: VisibleExportRow, index: int) -> bool:
        """Test the identity of an export permit, not the caller's page keys."""
        return (
            self._active()
            and 0 <= index < len(self._export_rows)
            and self._export_rows[index] is row
        )

    def _active(self) -> bool:
        # A copied Context can outlive its caller on the same thread.
        return (
            _CURRENT.get() is self
            and self._thread == get_ident()
            and not self._ended.is_set()
        )


_CURRENT: ContextVar[RenderSelection | None] = ContextVar(
    "pending_projection", default=None
)


def _read_batch(
    groups: dict[type[models.Model], set[UUID]], clinic_id: UUID | None
) -> list[tuple[object, ...]]:
    """UNION ALL scoped ORM reads; each model owns distinct typed output slots."""
    columns = [
        (f"pending_c{group}_{index}", model, field)
        for group, model in enumerate(groups)
        for index, field in enumerate(model._meta.concrete_fields)  # noqa: SLF001
    ]
    arms = []
    for index, (model, ids) in enumerate(groups.items()):
        source = rows(model).filter(pk__in=ids).order_by()
        if clinic_id is not None:
            source = source.filter(**{_clinic_binding(model): clinic_id})
        # A typed NULL in every other arm preserves each field's native database
        # type and normal Django converter, including its own encryption purpose.
        slots = {
            alias: models.F(field.attname)
            if owner is model
            else Cast(models.Value(None), field)
            for alias, owner, field in columns
        }
        arms.append(
            source.annotate(
                pending_render_group=models.Value(index), **slots
            ).values_list("pending_render_group", *slots)
        )
    return list(arms[0].union(*arms[1:], all=True))


def _clinic_binding(model: type[models.Model]) -> str:
    """Bind the row to the clinic whose choice lock protects this projection."""
    return {
        "comms.suggestedreply": "conversation__clinic_id",
        "scribe.transcriptversion": "session__clinic_id",
        "scribe.transcriptsegment": "session__clinic_id",
        "scribe.draftsectionsuggestion": "draft__clinic_id",
        "scribe.draftactionproposal": "draft__clinic_id",
    }.get(rows(model).query.get_meta().label_lower, "clinic_id")


def _fresh_records(
    selectors: Iterable[PendingKey[models.Model]], clinic_id: UUID | None
) -> dict[PendingKey[models.Model], models.Model]:
    """Fetch and convert all marked models in one ordinary RLS SELECT."""
    groups: dict[type[models.Model], set[UUID]] = defaultdict(set)
    for selector in selectors:
        if selector.model not in _registered():
            raise GatewayAccessDeniedError
        groups[selector.model].add(selector.pk)
    if not groups:
        return {}
    layout = []
    offset = 1  # column zero identifies the branch of the union
    for model in groups:
        names = [field.attname for field in model._meta.concrete_fields]  # noqa: SLF001
        layout.append((model, names, slice(offset, offset + len(names))))
        offset += len(names)
    result = {}
    for row in _read_batch(groups, clinic_id):
        model, names, span = layout[cast("int", row[0])]
        current = model.from_db(connection.alias, names, list(row[span]))
        result[key(current)] = current
    return result


def ai_pending_visible[R](
    selectors: Iterable[PendingKey[models.Model]] | ExportPage,
    project: Callable[[RenderSelection], R],
    *,
    clinic_id: UUID | None = None,
) -> R:
    """Lock before the final statement; keep its admission through serialization."""
    with nullcontext() if connection.in_atomic_block else transaction.atomic():
        records: dict[PendingKey[models.Model], models.Model] = {}
        export_data: list[tuple[str, str | dict[str, object]]] = []
        if isinstance(selectors, ExportPage):
            if not try_hold_organization_read():
                raise GatewayAccessDeniedError
            export_data = _export_page(selectors)
        else:
            selected = tuple(selectors)
            if selected and (
                try_hold_organization_read()
                if clinic_id is None
                else try_hold_decision(clinic_id=clinic_id, using=connection.alias)
            ):
                # Separate READ COMMITTED statement after the successful lock.
                records = _fresh_records(selected, clinic_id)
        handles = {k: VisiblePending(k, clinic_id, _ISSUER) for k in records}
        ended = Event()
        selection = RenderSelection(
            records,
            handles,
            export_data,
            tuple(VisibleExportRow(i, _ISSUER) for i in range(len(export_data))),
            _ISSUER,
            get_ident(),
            ended,
        )
        token = _CURRENT.set(selection)
        try:
            result = project(selection)
            # Never defer a pending projection to middleware or to a streaming
            # iterator after commit. Socket transmission is not part of rendering.
            if isinstance(result, SimpleTemplateResponse) and not result.is_rendered:
                result.render()
            if isinstance(result, HttpResponseBase) and result.streaming:
                raise GatewayAccessDeniedError
            return result
        finally:
            ended.set()
            _CURRENT.reset(token)
            records.clear()
            handles.clear()
            export_data.clear()


@cache
def _registered() -> frozenset[type[models.Model]]:
    return frozenset(
        model
        for model in apps.get_models()
        if issubclass(model, (PendingOutput, SealedOutput))
    )


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
    """Reauthorize a retained instance for a workflow command, not rendering."""
    visible = rows(type(instance)).filter(pk=instance.pk).first()
    if visible is None:
        raise GatewayAccessDeniedError
    return visible


def decisions_visible(
    selectors: Iterable[PendingKey[models.Model]],
    *,
    clinic_id: UUID,
    using: str = "default",
) -> bool:
    """Reauthorize all effects after a clinic-scoped lookup, not from a display permit.

    Check on the actor's actual connection, in a new statement *after* taking
    the fence. It lasts through the commit that writes the vote or decision.
    No model bytes or field permissions are conveyed by this Boolean.
    """
    selected = tuple(selectors)
    if not selected or any(k.model not in _registered() for k in selected):
        return False
    if not try_hold_decision(clinic_id=clinic_id, using=using):
        return False
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.enterprise_staff_context() AND "
            "bool_and(clinic_app.ai_pending_visible(relation,key) IS TRUE) "
            "FROM unnest(%s::text[],%s::uuid[]) AS selected(relation,key)",
            [
                [rows(k.model).query.get_meta().db_table for k in selected],
                [k.pk for k in selected],
            ],
        )
        result = cursor.fetchone()
        return result is not None and result[0] is True


def _export_page(page: ExportPage) -> list[tuple[str, str | dict[str, object]]]:
    # The resolver authenticates the requester and applies the same underlying
    # choice as staff RLS, but under the distinct organization export audience.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT row_key,row_data FROM clinic_app.interop_export_rows(%s,%s,%s,%s)",
            [page.export_id, page.relation, page.after, page.size],
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

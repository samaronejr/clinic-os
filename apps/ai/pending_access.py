"""Fenced data access for live models, including already populated ORM caches.

The database guard runs before materialization (including values and joined
columns). The instance/descriptor guard also covers data already in memory.
Neither makes a query or takes a lock to decide whether the fence is held.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache, wraps
from inspect import getattr_static
from threading import get_ident
from typing import TYPE_CHECKING, Any, Protocol, Self, cast, overload, override

from django.apps import apps
from django.core.exceptions import EmptyResultSet
from django.db import connections, models
from django.db.backends.signals import connection_created
from django.db.models.fields.related_descriptors import ForeignKeyDeferredAttribute
from django.db.models.fields.reverse_related import ForeignObjectRel

from apps.ai.contracts import GatewayAccessDeniedError
from apps.consent.decision_fence import (
    clinic_marker,
    decision_fenced,
    try_hold_decision,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator
    from uuid import UUID

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.models.base import ModelState
    from django.db.models.options import Options


class PendingReadOutsideVisibilityError(RuntimeError):
    """A renderer attempted an independent pending read behind the consent fence."""

    def __init__(self) -> None:
        """Never include record identities, protected values or generated SQL."""
        super().__init__("pending output requires typed visibility")


class _Lease:
    def __init__(self) -> None:
        self.thread = get_ident()
        self.open = True

    def live(self) -> bool:
        return self.open and self.thread == get_ident()


_loading: ContextVar[_Lease | None] = ContextVar("pending_batch_load", default=None)
_issued: ContextVar[tuple[_Lease, dict[int, models.Model]] | None] = ContextVar(
    "pending_batch_instances", default=None
)


class _Workflow(_Lease):
    """Only rows issued by this clinic's typed query may be read by its command."""

    def __init__(self, clinic: UUID) -> None:
        super().__init__()
        self.clinic = clinic
        self.records: dict[int, tuple[models.Model, object | None]] = {}


_workflow: ContextVar[_Workflow | None] = ContextVar("pending_workflow", default=None)


@dataclass
class _Inventory:
    tables: frozenset[str] = frozenset()
    identifiers: re.Pattern[str] | None = None
    issuer: object | None = None


_inventory = _Inventory()


def metadata[M: models.Model](model: type[M]) -> Options[M]:
    """Django installs one Options on each concrete class, including proxies."""
    return cast("Options[M]", vars(model)["_meta"])


def _state(row: models.Model) -> ModelState:
    return cast("ModelState", object.__getattribute__(row, "_state"))


class ProtectedRecord:
    """Prevent normal model serialization from reading cached columns via vars()."""

    def __getattribute__(self, name: str) -> object:
        """Subject even cached instance dictionaries to the pending read check."""
        if name == "__dict__":
            _check_instance(cast("models.Model", self))
        return super().__getattribute__(name)


def selector_pk[M: models.Model](row: M) -> UUID:
    """Discard everything except the already held selector; never fetch a row."""
    fields = object.__getattribute__(row, "__dict__")
    return cast("UUID", fields[metadata(type(row)).pk.attname])


def clinic_binding(model: type[models.Model]) -> str:
    """Bind reads to exactly the scope protected by the clinic lock."""
    return {
        "comms.suggestedreply": "conversation__clinic_id",
        "scribe.transcriptversion": "session__clinic_id",
        "scribe.transcriptsegment": "session__clinic_id",
        "scribe.draftsectionsuggestion": "draft__clinic_id",
        "scribe.draftactionproposal": "draft__clinic_id",
        "interop.organizationexportpart": "export__clinic_id",
    }.get(metadata(model).label_lower, "clinic_id")


@contextmanager
def _construction() -> Iterator[None]:
    lease = _Lease()
    token = _loading.set(lease)
    try:
        yield
    finally:
        lease.open = False
        _loading.reset(token)


@contextmanager
def construct_batch(issuer: object) -> Iterator[None]:
    """Boundary-owned construction only. Never span a user projection callback."""
    _require_issuer(issuer)
    with _construction():
        yield


def _require_issuer(issuer: object) -> None:
    if _inventory.issuer is None or _inventory.issuer is not issuer:
        raise PendingReadOutsideVisibilityError


@contextmanager
def project_instances(
    issuer: object, records: Iterable[models.Model]
) -> Iterator[None]:
    """Admit only the instances in this batch, not another load of the same PK."""
    _require_issuer(issuer)
    lease = _Lease()
    rows = {id(r): r for r in records}
    for row in rows.values():
        vars(_state(row))["pending_frame"] = lease
    token = _issued.set((lease, rows))
    workflow = _workflow.set(None)
    try:
        yield
    finally:
        rows.clear()
        lease.open = False
        _issued.reset(token)
        _workflow.reset(workflow)


def _restricted() -> bool:
    if not decision_fenced():
        return False
    loading = _loading.get()
    return not (loading is not None and loading.live())


def _check_instance(instance: models.Model) -> None:
    frame = vars(_state(instance)).get("pending_frame")
    if frame is not None:
        current = _issued.get()
        if (
            not frame.live()
            or current is None
            or current[0] is not frame
            or current[1].get(id(instance)) is not instance
        ):
            raise PendingReadOutsideVisibilityError
        return
    if _restricted() and not _command_record(instance):
        raise PendingReadOutsideVisibilityError


@contextmanager
def workflow(clinic_id: UUID) -> Iterator[None]:
    """Bound command queries, never arbitrary ORM queries or cached relations."""
    parent = _workflow.get()
    if parent is not None and parent.live() and parent.clinic == clinic_id:
        yield
        return
    lease = _Workflow(clinic_id)
    token = _workflow.set(lease)
    try:
        yield
    finally:
        lease.open = False
        lease.records.clear()
        _workflow.reset(token)


def clinic_workflow[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Scope a keyword-only clinic command; discard its row handles at return."""

    @wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        with workflow(cast("UUID", kwargs["clinic_id"])):
            return fn(*args, **kwargs)

    return run


def _marker(lease: _Workflow, using: str) -> object | None:
    if not decision_fenced():
        return None
    marker = clinic_marker(lease.clinic, using)
    if marker is None:
        if not try_hold_decision(clinic_id=lease.clinic, using=using):
            raise PendingReadOutsideVisibilityError
        marker = clinic_marker(lease.clinic, using)
    return marker


def _bind(lease: _Workflow, row: models.Model, stamp: object | None) -> None:
    record = lease.records.get(id(row))
    if (
        record is not None
        and record[0] is row
        and (record[1] is stamp or stamp is None)
    ):
        return
    lease.records[id(row)] = (row, stamp)
    for linked in _state(row).fields_cache.values():
        if (
            isinstance(linked, models.Model)
            and metadata(type(linked)).db_table in _inventory.tables
        ):
            _bind(lease, linked, None)


def _command_record(row: models.Model) -> bool:
    lease = _workflow.get()
    if lease is None or not lease.live():
        return False
    owned = lease.records.get(id(row))
    if owned is None or owned[0] is not row:
        return False
    db = _state(row).db or "default"
    stamp = _marker(lease, db)
    if owned[1] is not stamp:
        # The first lock was acquired after the original command lookup (or
        # rolled back to a savepoint). Re-read this exact row under FORCE RLS;
        # the previously loaded data is not display or command authority.
        with _construction():
            meta = metadata(type(row))
            fields = [f.attname for f in meta.concrete_fields if f.attname in vars(row)]
            manager = meta.default_manager
            if manager is None:
                raise PendingReadOutsideVisibilityError
            fresh = (
                manager.using(db)
                .only(*fields)
                .filter(pk=row.pk, **{clinic_binding(type(row)): lease.clinic})
                .first()
            )
            if fresh is None:
                raise GatewayAccessDeniedError
            for name in fields:
                vars(row)[name] = vars(fresh)[name]
            _state(row).fields_cache.clear()
        _bind(lease, row, stamp)
    return True


class CommandRows[M: models.Model](models.QuerySet[M]):
    """RLS/clinic-filtered reads scoped to the command, not a renderer."""

    @contextmanager
    def _evaluation(self) -> Iterator[tuple[_Workflow, object | None]]:
        # Django query.chain() copies per-query state when filters are cloned.
        lease = _workflow.get()
        bound: object = vars(self.query).get("pending_lease")
        if lease is None or not lease.live() or bound is not lease:
            raise PendingReadOutsideVisibilityError
        stamp = _marker(lease, self.db)
        with _construction():
            # Do not re-label scalars (or instances) cached before a fence was
            # acquired or rolled back as if they came from the new snapshot.
            if (
                self._result_cache is not None
                and vars(self).get("pending_snapshot") is not stamp
            ):
                self._result_cache = None
            vars(self)["pending_snapshot"] = stamp
            yield lease, stamp

    @overload
    def __getitem__(self, index: int) -> M: ...

    @overload
    def __getitem__(self, index: slice) -> Self: ...

    def __getitem__(self, index: int | slice) -> M | Self:
        """Django bypasses _fetch_all when an indexed result is already cached."""
        with self._evaluation():
            return super().__getitem__(index)

    def _fetch_all(self) -> None:
        with self._evaluation() as (lease, stamp):
            super()._fetch_all()
            for row in self._result_cache or ():
                if isinstance(row, models.Model):
                    _bind(lease, row, stamp)

    def exists(self) -> bool:
        """Observe existence only on the scoped, current query."""
        with self._evaluation():
            return super().exists()

    def count(self) -> int:
        """Count only within the owning workflow."""
        with self._evaluation():
            return super().count()

    def aggregate(self, *args: object, **kwargs: object) -> dict[str, Any]:
        """Allow only the scoped command's aggregation, never a fresh raw query."""
        with self._evaluation():
            return super().aggregate(*args, **kwargs)

    def create(self, **kwargs: object) -> M:
        """Keep an inserted instance bound to the operation that created it."""
        with self._evaluation() as (lease, stamp):
            row = super().create(**kwargs)
            _bind(lease, row, stamp)
            return row

    @override
    def bulk_create(
        self,
        objs: Iterable[M],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[M]:
        """Insert already constructed records on the scoped writer's connection."""
        with self._evaluation() as (lease, stamp):
            stored = super().bulk_create(
                objs,
                batch_size,
                ignore_conflicts,
                update_conflicts,
                update_fields,
                unique_fields,
            )
            for row in stored:
                _bind(lease, row, stamp)
            return stored

    def update(self, **kwargs: object) -> int:
        """Preserve RLS/clinic restrictions on command transitions."""
        with self._evaluation():
            return super().update(**kwargs)


def command_rows[M: models.Model](
    qs: models.QuerySet[M], clinic_field: str
) -> models.QuerySet[M]:
    """Use a typed command capability only during an explicitly scoped workflow."""
    lease = _workflow.get()
    if lease is None or not lease.live():
        return qs
    bound: CommandRows[M] = CommandRows(
        model=qs.model, query=qs.query.chain(), using=qs.db
    )
    vars(bound.query)["pending_lease"] = lease
    return bound.filter(**{clinic_field: lease.clinic})


class _Descriptor(Protocol):
    def __get__(
        self, instance: models.Model | None, owner: type[models.Model]
    ) -> object: ...


class _ProtectedAttribute:
    """A data descriptor: a value in ``__dict__`` cannot shadow its check."""

    def __init__(self, name: str, original: _Descriptor, *, relation: bool) -> None:
        self.name, self.original, self.relation = name, original, relation

    def __get__(
        self, instance: models.Model | None, owner: type[models.Model]
    ) -> object:
        if instance is None:
            return self.original.__get__(None, owner)
        if self.relation and _restricted():
            # An admitted parent never implicitly admits its related model,
            # even when select_related/prefetch populated the relation cache.
            if not _command_record(instance):
                raise PendingReadOutsideVisibilityError
            # Independently authorize the related row, not an arbitrary lazy
            # manager/queryset. Do not trust the parent's select_related cache.
            with _construction():
                related = self.original.__get__(instance, owner)
            if not isinstance(related, models.Model):
                raise PendingReadOutsideVisibilityError
            lease = _workflow.get()
            if lease is None:
                raise PendingReadOutsideVisibilityError
            _bind(lease, related, None)
            _check_instance(related)
            return related
        _check_instance(instance)
        if not hasattr(type(self.original), "__set__") and self.name in vars(instance):
            return vars(instance)[self.name]
        return self.original.__get__(instance, owner)

    def __set__(self, instance: models.Model, value: object) -> None:
        # A command may write its first field immediately after taking the
        # fence. Refresh before that assignment, never over unsaved changes.
        if _restricted():
            lease = _workflow.get()
            if lease is not None and id(instance) in lease.records:
                _check_instance(instance)
        if isinstance(self.original, ForeignKeyDeferredAttribute):
            # Match Django's FK-id assignment, including cached-relation
            # invalidation, without exposing the instance dictionary to a reader.
            values = object.__getattribute__(instance, "__dict__")
            field = self.original.field
            if values.get(self.name) != value and field.is_cached(instance):
                field.delete_cached_value(instance)
            values[self.name] = value
            return
        setter = getattr(self.original, "__set__", None)
        if setter is not None:
            setter(instance, value)
        else:
            # Model.__init__ assigns fields before a new instance can enter the
            # command's result set. Storage for a write is not an outward read.
            object.__getattribute__(instance, "__dict__")[self.name] = value

    def __delete__(self, instance: models.Model) -> None:
        del vars(instance)[self.name]


def _guard_sql(
    execute: Callable[..., object],
    sql: object,
    params: object,
    many: bool,
    context: object,
) -> object:
    if _restricted() and _inventory.identifiers is not None:
        statement = str(sql)
        if _inventory.identifiers.search(statement) and not (
            statement.startswith(("UPDATE ", "INSERT INTO ", "DELETE FROM "))
            and not re.search(
                r"\b(?:SELECT|RETURNING|USING|WITH)\b|;", statement, re.IGNORECASE
            )
        ):
            raise PendingReadOutsideVisibilityError
    return execute(sql, params, many, context)


def _connect(sender: object, connection: BaseDatabaseWrapper, **kwargs: object) -> None:
    del sender, kwargs
    if _guard_sql not in connection.execute_wrappers:
        connection.execute_wrappers.append(_guard_sql)


class _ResultCache[Row](models.QuerySet[models.Model, Row]):
    """A previously evaluated values queryset has no model descriptors left."""

    def check_results(self) -> None:
        if not _restricted():
            return
        qs = cast("models.QuerySet[models.Model]", self)
        state = vars(self)
        if "pending_dependencies" not in state:
            pending = metadata(qs.model).db_table in _inventory.tables
            if not pending and _inventory.identifiers is not None:
                try:
                    # Inspect the real compiled SELECT, including joined
                    # columns, scalar subqueries and UNION arms. No DB read.
                    sql, _ = qs.query.get_compiler(using=qs.db).as_sql()
                except EmptyResultSet:
                    pass
                else:
                    pending = _inventory.identifiers.search(sql) is not None
            state["pending_dependencies"] = pending
        if state["pending_dependencies"]:
            raise PendingReadOutsideVisibilityError

    def __iter__(self) -> Iterator[Row]:
        """Recheck on yield even when the iterator was opened before the fence."""
        items = super().__iter__()
        for row in items:
            self.check_results()
            yield row

    def iterator(self, chunk_size: int | None = None) -> Iterator[Row]:
        """Guard prefetched cursor chunks as well as the initial SQL execution."""
        for row in super().iterator(chunk_size):
            self.check_results()
            yield row

    @property
    def _result_cache(self) -> list[Any] | None:
        cache = vars(self).get("_pending_cache")
        if cache is not None:
            self.check_results()
        return cast("list[Any] | None", cache)

    @_result_cache.setter
    def _result_cache(self, value: list[Any] | None) -> None:
        vars(self)["_pending_cache"] = value


@cache
def _query_type(cls: type) -> type:
    return type("Fenced" + cls.__name__, (_ResultCache, cls), {})


def _descriptors(
    model: type[models.Model], protected: frozenset[type[models.Model]]
) -> dict[str, bool]:
    attrs: dict[str, bool] = {}
    for field in metadata(model).get_fields():
        if model in protected and field.concrete:
            attrs[field.attname] = attrs[field.name] = False
        if field.is_relation and field.related_model in protected:
            name = (
                field.get_accessor_name()
                if isinstance(field, ForeignObjectRel)
                else field.name
            )
            if name and hasattr(model, name):
                attrs[name] = True
    if model in protected:
        attrs.update(
            (n, False)
            for parent in model.__mro__
            for n, v in vars(parent).items()
            if isinstance(v, property) and n != "pk"
        )
    return attrs


def install(protected: frozenset[type[models.Model]], *, issuer: object) -> None:
    """Instrument live model metadata; never historical StateApps or migrations."""
    if _inventory.identifiers is not None:
        _require_issuer(issuer)
        return
    _inventory.issuer = issuer
    _inventory.tables = frozenset(metadata(m).db_table for m in protected)
    _inventory.identifiers = re.compile(
        r"\b(?:" + "|".join(map(re.escape, sorted(_inventory.tables))) + r")\b",
        re.IGNORECASE,
    )
    for model in apps.get_models():
        meta = metadata(model)
        for manager in (*meta.managers, meta.base_manager):
            cls = getattr_static(manager, "_queryset_class")
            if not issubclass(cls, _ResultCache):
                vars(manager)["_queryset_class"] = _query_type(cls)
        for name, relation in _descriptors(model, protected).items():
            descriptor = getattr_static(model, name)
            if not isinstance(descriptor, _ProtectedAttribute):
                setattr(
                    model,
                    name,
                    _ProtectedAttribute(name, descriptor, relation=relation),
                )
    connection_created.connect(
        _connect, dispatch_uid="ai.pending_visibility_data_guard"
    )
    for db in connections.all(initialized_only=True):
        _connect(None, db)

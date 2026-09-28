"""Observe actual SQL, permission accessors and loaded authority-model fields.

No authorization result is replaced. Catalog closure rejects opaque SQL, and
same-backend transaction counters independently witness physical relation use.
"""

from __future__ import annotations

import inspect
import sys
from functools import wraps
from typing import TYPE_CHECKING, cast

import psycopg
import pytest
from apps.identity import current_context
from apps.identity.models import User
from django.apps import apps
from django.db import connection, connections
from django.db.models import Model
from psycopg.pq import TransactionStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType, FrameType

    from psycopg.rows import TupleRow

    from identity.authority_catalog import Catalog, Channels

Touch = tuple[str, str]


class AuthorityObservedError(AssertionError):
    """An observing or uninspectable callable cannot be certified nonstaff."""


class AuthorityObserver:
    def __init__(self, catalog: Catalog, channels: Channels) -> None:
        self.catalog = catalog
        self.channels = channels
        self.touches: set[Touch] = set()
        self.raw = connection.connection
        assert self.raw is not None
        self._pending: dict[
            int, tuple[psycopg.Connection[TupleRow], dict[int, tuple[int, ...]]]
        ] = {}
        self._transactions: set[int] = set()
        self._busy = False
        self._monitor_id: int | None = None
        self._accessor_objects = {
            id(value): current_context.__name__ + "." + name
            for name, value in vars(current_context).items()
            if callable(value)
            and getattr(value, "__module__", "") == current_context.__name__
        }
        self._accessors = {
            function.__code__: f"{current_context.__name__}.{name}"
            for name, function in inspect.getmembers(
                current_context, inspect.isfunction
            )
            if function.__module__ == current_context.__name__
        }
        for name, member in vars(User).items():
            function = member.fget if isinstance(member, property) else member
            if inspect.isfunction(function):
                self._accessors[function.__code__] = f"apps.identity.models.User.{name}"
        self._execute_codes = {
            cls.execute.__code__
            for cls in (psycopg.Cursor, psycopg.ClientCursor, psycopg.ServerCursor)
        }
        self._unsupported_codes = {
            inspect.unwrap(method).__code__
            for cls in (psycopg.Cursor, psycopg.ClientCursor, psycopg.ServerCursor)
            for method in (cls.executemany, cls.copy, cls.stream)
        }
        self._transaction_codes = {
            psycopg.Connection.commit.__code__,
            psycopg.Connection.rollback.__code__,
        }
        self._fields = {}
        for model in apps.get_models():
            relation = next(
                (
                    r
                    for r in catalog.relations.values()
                    if r.oid in channels.relations
                    and r.name == "clinic_app." + model._meta.db_table
                ),
                None,
            )
            if relation is not None:
                self._fields[model] = {
                    f.attname: relation.name + "." + f.column
                    for f in model._meta.concrete_fields
                    if f.column in relation.columns
                }
        self._original_getattribute = Model.__getattribute__
        self._patch = pytest.MonkeyPatch()

    def _observed(self, raw: object) -> psycopg.Connection[TupleRow] | None:
        # The staff backend and the registered machine-login alias are both
        # observed backends; any other connection stays opaque.
        if raw is self.raw or (
            "agent" in connections and raw is connections["agent"].connection
        ):
            return cast("psycopg.Connection[TupleRow]", raw)
        return None

    def _stats(self, raw: psycopg.Connection[TupleRow]) -> dict[int, tuple[int, ...]]:
        with raw.cursor() as cursor:
            cursor.execute(
                "SELECT relid, seq_scan, COALESCE(idx_scan,0), "
                "seq_tup_read, COALESCE(idx_tup_fetch,0) "
                "FROM pg_stat_xact_user_tables WHERE relid = ANY(%s)",
                [list(self.channels.relations)],
            )
            return {row[0]: tuple(row[1:]) for row in cursor.fetchall()}

    def _sql(self, frame: FrameType) -> None:
        cursor = frame.f_locals["self"]
        raw = self._observed(cursor.connection)
        if raw is None:
            self.touches.add(("opaque", "SQL on an unobserved connection"))
            return
        try:
            with psycopg.ClientCursor(raw) as renderer:
                text = renderer.mogrify(
                    frame.f_locals["query"], frame.f_locals.get("params")
                )
            reads = self.catalog.statement(text)
        except (psycopg.Error, ValueError, TypeError) as error:
            self.touches.add(("opaque", "unrenderable SQL: " + type(error).__name__))
            return
        self.touches.update(
            ("relation", self.catalog.relations[oid].name)
            for oid in reads.relations & self.channels.relations
        )
        self.touches.update(
            ("guc", name) for name in reads.settings & self.channels.settings
        )
        self.touches.update(
            ("function", self.catalog.functions[oid].name)
            for oid in reads.functions & self.channels.functions
        )
        self.touches.update(("opaque", reason) for reason in reads.opaque)
        if raw.info.transaction_status != TransactionStatus.INERROR:
            self._pending[id(frame)] = (raw, self._stats(raw))

    def _after_sql(self, frame: FrameType) -> None:
        pending = self._pending.pop(id(frame), None)
        if pending is None:
            return
        raw, before = pending
        if raw.info.transaction_status == TransactionStatus.INERROR:
            self.touches.add(
                ("opaque", "aborted SQL has no readable transaction counters")
            )
            return
        after = self._stats(raw)
        self.touches.update(
            ("relation", self.catalog.relations[oid].name)
            for oid, counts in after.items()
            if counts != before.get(oid, (0,) * len(counts))
        )

    def profile(self, frame: FrameType, event: str, argument: object) -> None:
        self._busy = True
        try:
            self._profile(frame, event, argument)
        finally:
            self._busy = False

    def _profile(self, frame: FrameType, event: str, argument: object) -> None:
        code = frame.f_code
        if event == "call":
            if code in self._accessors:
                self.touches.add(("python", self._accessors[code]))
            if code in self._execute_codes:
                self._sql(frame)
            if code in self._unsupported_codes:
                self.touches.add(("opaque", "uninspectable cursor operation"))
            if code in self._transaction_codes:
                self._transactions.add(id(frame))
        elif event == "return":
            if code in self._execute_codes:
                self._after_sql(frame)
            self._transactions.discard(id(frame))
        elif event == "c_call":
            name = getattr(argument, "__name__", "")
            if name in {
                "exec_",
                "exec_params",
                "exec_prepared",
                "send_query",
                "send_query_params",
                "send_query_prepared",
            } and not (self._pending or self._transactions):
                self.touches.add(("opaque", "SQL bypassed the observed cursor"))

    def _call(
        self, code: CodeType, offset: int, function: object, argument: object
    ) -> None:
        if self._busy:
            return
        accessor = self._accessor_objects.get(id(function))
        if accessor is not None:
            self.touches.add(("python", accessor))
        if getattr(function, "__name__", "") in {
            "exec_",
            "exec_params",
            "exec_prepared",
            "send_query",
            "send_query_params",
            "send_query_prepared",
        } and not (self._pending or self._transactions):
            self.touches.add(("opaque", "SQL bypassed the observed cursor"))
        if code.co_filename != __file__:
            controls = (
                sys.setprofile,
                sys.monitoring.set_events,
                sys.monitoring.set_local_events,
                sys.monitoring.register_callback,
                sys.monitoring.free_tool_id,
            )
            if any(function is control for control in controls):
                self.touches.add(("opaque", "observer controls accessed"))
            if (
                getattr(function, "__name__", "") == "__getattribute__"
                and type(argument) in self._fields
            ):
                self.touches.add(("opaque", "unmediated authority-model attributes"))

    def install(self) -> None:
        # CALL sees Cython/native methods and prebound aliases that setprofile's
        # c_call omits. Tool ownership is scoped and never displaces coverage.
        free = next(
            (i for i in range(5, -1, -1) if sys.monitoring.get_tool(i) is None), None
        )
        if free is None:
            message = "no monitoring tool slot available"
            raise AuthorityObservedError(message)
        self._monitor_id = free
        sys.monitoring.use_tool_id(free, "staff-authority-observer")
        sys.monitoring.register_callback(free, sys.monitoring.events.CALL, self._call)
        sys.monitoring.set_events(free, sys.monitoring.events.CALL)
        original = self._original_getattribute
        observer = self

        def attribute(instance: Model, name: str) -> object:
            # Retain descriptors and database fetches; do not substitute data.
            fields = observer._fields.get(type(instance), {})
            field = fields.get(name)
            if field is not None:
                observer.touches.add(("attribute", field))
            if fields and name == "__dict__":
                observer.touches.add(("opaque", "raw authority-model attributes"))
            return cast("object", original(instance, name))

        self._patch.setattr(Model, "__getattribute__", attribute)
        classes = (
            psycopg.Connection,
            psycopg.ConnectionInfo,
            psycopg.Cursor,
            psycopg.ClientCursor,
            psycopg.ServerCursor,
        )
        originals = {cls: cls.__getattribute__ for cls in classes}
        for cls, getter in originals.items():
            self._patch.setattr(cls, "__getattribute__", self._libpq_access(getter))
        for name, function in inspect.getmembers(current_context, inspect.isfunction):
            if function.__module__ == current_context.__name__:
                self._patch.setattr(
                    current_context, name, self._recording(function, name)
                )

    def _libpq_access(
        self, original: Callable[[object, str], object]
    ) -> Callable[[object, str], object]:
        def attribute(instance: object, name: str) -> object:
            if name in {"pgconn", "_pgconn"}:
                caller = sys._getframe(1).f_globals.get("__name__", "")
                if not caller.startswith(("psycopg", "django.db")):
                    self.touches.add(("opaque", "direct libpq handle access"))
            return original(instance, name)

        return attribute

    def restore(self) -> None:
        # A generator contextmanager rewrites exception tracebacks; some domain
        # exceptions are frozen dataclasses. Manual finally preserves their type.
        if self._monitor_id is not None:
            sys.monitoring.set_events(self._monitor_id, 0)
            sys.monitoring.register_callback(
                self._monitor_id, sys.monitoring.events.CALL, None
            )
            sys.monitoring.free_tool_id(self._monitor_id)
            self._monitor_id = None
        self._patch.undo()

    def _recording(
        self, function: Callable[..., object], name: str
    ) -> Callable[..., object]:
        @wraps(function)
        def invoke(*args: object, **kwargs: object) -> object:
            self.touches.add(("python", current_context.__name__ + "." + name))
            return function(*args, **kwargs)

        return invoke

    def assert_nonstaff(self, symbol: str) -> None:
        if self.touches:
            raise AuthorityObservedError(symbol, sorted(self.touches))

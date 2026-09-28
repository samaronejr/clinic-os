"""Outermost test-handler observation, independent receipts and tamper checks.

Boundary: this guard defends against accidental or fixture-level disablement
and against unobserved refusal session writes. Replacing, unwrapping or
rewriting what it installed (its methods, the frozen check and scope, their
code, defaults, closures, namespaces and the builtins they resolve), bypassing
its middleware, or a test whose client requests were not all checked, fails
the test that did it. Deliberately adversarial in-process code is outside that
boundary, because any Python in the process can rewrite any Python object. The
fail-closed static scan over ``tests/``
(``tests/core/test_workspace_guard_static.py``) rejects the plainly written
forms (stash keys, handler chain, guard internals, function-state writes,
patching the guard or ``request_started``); deliberately aliased or
introspective chains are covered only by code review.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import asdict, dataclass
from functools import wraps
from itertools import count
from time import perf_counter_ns
from types import CodeType, FunctionType, MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Protocol, cast
from weakref import WeakKeyDictionary, ref

import pytest
from django.core import signals
from django.test import AsyncClient, Client
from django.test.client import AsyncClientHandler, ClientHandler
from django.utils.deprecation import MiddlewareMixin

from workspace_refusal_branches import RefusalTrace
from workspace_refusal_support import (
    RefusalGuardStats,
    bind_refusal_check,
    check_refusal,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from django.core.handlers.base import BaseHandler
    from django.http import HttpRequest, HttpResponseBase

    from workspace_refusal_support import RefusalCheck

    type ResponseHandler = (
        Callable[[HttpRequest], HttpResponseBase]
        | Callable[[HttpRequest], Awaitable[HttpResponseBase]]
    )


class _HandlerChain(Protocol):
    _middleware_chain: object


_EMPTY_CELL = object()


def _cell(cell: object) -> object:
    try:
        return cast("_Cell", cell).cell_contents
    except ValueError:
        return _EMPTY_CELL


class _Cell(Protocol):
    cell_contents: object


def _global_names(code: CodeType) -> set[str]:
    names = set(code.co_names)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names |= _global_names(constant)
    return names


def _function(value: object) -> FunctionType:
    assert isinstance(value, FunctionType), value
    return value


def _builtins(namespace: Mapping[str, object]) -> Mapping[str, object]:
    value = namespace.get("__builtins__", {})
    return (
        vars(value)
        if isinstance(value, ModuleType)
        else cast("dict[str, object]", value)
    )


@dataclass(frozen=True)
class _FunctionState:
    """Everything that decides what a function does, captured by content."""

    function: FunctionType
    code: CodeType
    defaults: object
    kwdefaults: object
    kwdefault_items: tuple[tuple[str, object], ...]
    closure: object
    cells: tuple[object, ...]
    namespace: dict[str, object]
    bindings: tuple[tuple[str, object], ...]
    builtin_bindings: tuple[tuple[str, object], ...]
    whole_namespace: bool

    @classmethod
    def capture(cls, function: FunctionType, *, whole: bool) -> _FunctionState:
        namespace = function.__globals__
        read = _global_names(function.__code__)
        names = set(namespace) if whole else read | {"__builtins__"}
        builtins = _builtins(namespace)
        return cls(
            function=function,
            code=function.__code__,
            defaults=function.__defaults__,
            kwdefaults=function.__kwdefaults__,
            kwdefault_items=tuple((function.__kwdefaults__ or {}).items()),
            closure=function.__closure__,
            cells=tuple(_cell(cell) for cell in function.__closure__ or ()),
            namespace=namespace,
            bindings=tuple(
                (name, namespace[name]) for name in sorted(names) if name in namespace
            ),
            # Names the code reads that resolve through builtins, not globals.
            builtin_bindings=tuple(
                (name, builtins[name])
                for name in sorted(read)
                if name not in namespace and name in builtins
            ),
            whole_namespace=whole,
        )

    def changed(self) -> list[str]:
        function = self.function
        changed = []
        if function.__code__ is not self.code:
            changed.append("__code__")
        if function.__defaults__ is not self.defaults:
            changed.append("__defaults__")
        kwdefaults = function.__kwdefaults__
        if kwdefaults is not self.kwdefaults or [
            (name, value) for name, value in (kwdefaults or {}).items()
        ] != [(name, value) for name, value in self.kwdefault_items]:
            changed.append("__kwdefaults__")
        closure = function.__closure__
        if closure is not self.closure or any(
            _cell(cell) is not value
            for cell, value in zip(closure or (), self.cells, strict=True)
        ):
            changed.append("__closure__")
        namespace = function.__globals__
        if (
            namespace is not self.namespace
            or any(
                name not in namespace or namespace[name] is not value
                for name, value in self.bindings
            )
            or (self.whole_namespace and len(namespace) != len(self.bindings))
        ):
            changed.append("__globals__")
        builtins = _builtins(namespace)
        if any(
            name in namespace or builtins.get(name) is not value
            for name, value in self.builtin_bindings
        ):
            changed.append("__builtins__")
        return changed


OBSERVED_ATTRIBUTE = "_workspace_refusal_observation"
STARTED_UID = "workspace-refusal-client-started"
RECEIPT_KEY = "clinic.workspace_refusal_receipt"


class RefusalMiddleware(MiddlewareMixin):
    """Observe after every production middleware, including SessionMiddleware."""

    def __init__(
        self,
        get_response: ResponseHandler,
        observer: RefusalObserver,
        check: RefusalCheck,
    ) -> None:
        super().__init__(get_response)
        self.observer = observer
        self.check = check

    def process_request(self, request: HttpRequest) -> None:
        self.observer.trace.begin_request()

    def process_response(
        self, request: HttpRequest, response: HttpResponseBase
    ) -> HttpResponseBase:
        self.observer.trace.finish_request(response.status_code)
        setattr(response, OBSERVED_ATTRIBUTE, self.observer)
        try:
            self.check(request, response, self.observer.stats)
        finally:
            self.observer.evaluated(request)
        return response


class RefusalObserver:
    """Install on actual Django handlers; fail closed if a fixture changes it."""

    def __init__(self) -> None:
        self.stats = RefusalGuardStats()
        self.trace = RefusalTrace()
        self.patch = pytest.MonkeyPatch()
        self._installed: dict[tuple[type[object], str], object] = {}
        self.expected: Mapping[tuple[type[object], str], object] = {}
        self.chains: WeakKeyDictionary[BaseHandler, tuple[ref[object], ref[object]]] = (
            WeakKeyDictionary()
        )
        self.tampering: list[str] = []
        self.receipts = count(1)
        self.started_receipts: Counter[int] = Counter()
        self.evaluated_receipts: Counter[int] = Counter()
        self.accounted_through = 0
        self.check: RefusalCheck | None = None
        self.dependencies: Mapping[str, tuple[object, str, object]] = {}
        self.function_states: tuple[_FunctionState, ...] = ()

    def _install(self, owner: type[object], name: str, value: object) -> None:
        self.patch.setattr(owner, name, value)
        self._installed[owner, name] = value

    def _bind_dependencies(self) -> RefusalCheck:
        check, dependencies = bind_refusal_check()
        self.check = check
        module = sys.modules[__name__]
        for name, value in vars(module).items():
            if getattr(value, "__module__", None) == check_refusal.__module__:
                dependencies[f"{__name__}.{name}"] = (module, name, value)
        # Read-only from here on; the only reference to the dict is the proxy.
        self.dependencies = MappingProxyType(dependencies)
        # Every observer method and every guard-owned middleware method is a
        # dependency too: a later class patch cannot silently alter the path.
        for owner in (RefusalMiddleware, RefusalObserver, RefusalTrace, _FunctionState):
            for name, value in vars(owner).items():
                if callable(value) and (
                    not name.startswith("__") or name in {"__init__", "__call__"}
                ):
                    self._installed[owner, name] = value
        self._installed[MiddlewareMixin, "__call__"] = MiddlewareMixin.__call__
        return check

    def _freeze(self) -> None:
        """Record code, defaults, closures and globals of every guard function.

        The frozen check and scope are recorded with their whole namespace;
        every installed wrapper, observer method and helper of this module
        (including the recorder itself) with the globals and builtins it reads.
        The installed map becomes read-only; only its proxy references it.
        """
        self.expected = MappingProxyType(self._installed)
        del self._installed
        assert isinstance(self.check, FunctionType)
        frozen = [self.check, _function(self.check.__globals__["workspace_scope"])]
        module = [
            member
            for value in vars(sys.modules[__name__]).values()
            if getattr(value, "__module__", None) == __name__
            for member in (
                vars(value).values() if isinstance(value, type) else (value,)
            )
        ]
        installed = [
            function
            for value in (*self.expected.values(), *module)
            for function in (getattr(value, "__func__", value),)
            if isinstance(function, FunctionType) and function not in frozen
        ]
        self.function_states = (
            *(_FunctionState.capture(function, whole=True) for function in frozen),
            *(
                _FunctionState.capture(function, whole=False)
                for function in dict.fromkeys(installed)
            ),
        )
        assert len(self.function_states) > len(frozen)

    def seal(self) -> tuple[tuple[str, object, str, object], ...]:
        """Return ``(label, holder, attribute, value)`` for the conftest root.

        The conftest hooks hold these outside this object and compare them
        after every test, so replacing the recorded state or rewriting the
        verifier itself still shows up.
        """
        verifiers = (
            RefusalObserver.integrity_errors,
            RefusalObserver.session_errors,
            RefusalObserver.count_errors,
            RefusalObserver.counters,
            _FunctionState.changed,
            _builtins,
            _cell,
        )
        attributes = ("__class__", "check", "dependencies", "expected")
        return (
            *(
                (f"RefusalObserver.{name}", self, name, getattr(self, name))
                for name in (*attributes, "function_states", "stats")
            ),
            *(
                (f"{function.__qualname__}.__code__", function, "__code__", code)
                for function in map(_function, verifiers)
                for code in (function.__code__,)
            ),
        )

    def counters(self) -> tuple[int, int, int]:
        stats = self.stats
        return stats.client_requests, stats.evaluated_requests, stats.responses

    def count_errors(self, before: tuple[int, int, int]) -> list[str]:
        """One test's requests, evaluated responses and checked responses agree."""
        requests, evaluated, responses = (
            now - then for now, then in zip(self.counters(), before, strict=True)
        )
        if requests == evaluated == responses:
            return []
        return [
            "Test client requests, evaluated and checked responses differ: "
            f"requests={requests} evaluated={evaluated} checked={responses}"
        ]

    def start(self) -> None:
        self.trace.start()
        check = self._bind_dependencies()
        signals.request_started.connect(
            self.started, weak=False, dispatch_uid=STARTED_UID
        )
        original_request = Client.request
        original_async_request = AsyncClient.request

        # These sentinels preserve the client's API, but do NOT observe responses.
        # Unwrapping one cannot remove the middleware or independent signal count.
        @wraps(original_request)
        def request(client: Client, **kwargs: object) -> HttpResponseBase:
            return original_request(client, **kwargs)

        @wraps(original_async_request)
        async def async_request(
            client: AsyncClient, **kwargs: object
        ) -> HttpResponseBase:
            return await original_async_request(client, **kwargs)

        self._install(Client, "request", request)
        self._install(AsyncClient, "request", async_request)
        original_load = ClientHandler.load_middleware

        def load(handler: BaseHandler, is_async: bool = False) -> None:
            original_load(handler, is_async=is_async)
            state = cast("_HandlerChain", handler)
            chain = cast("ResponseHandler", state._middleware_chain)
            middleware = RefusalMiddleware(chain, self, check)
            state._middleware_chain = middleware
            self.chains[handler] = ref(middleware), ref(chain)

        self._install(ClientHandler, "load_middleware", load)
        self._install(AsyncClientHandler, "load_middleware", load)
        original_call = ClientHandler.__call__
        original_async_call = AsyncClientHandler.__call__

        def call(
            handler: ClientHandler, environ: dict[str, object]
        ) -> HttpResponseBase:
            response = original_call(handler, environ)
            self.require_receipt(response)
            return response

        async def async_call(
            handler: AsyncClientHandler, scope: dict[str, object]
        ) -> HttpResponseBase:
            response = await original_async_call(handler, scope)
            self.require_receipt(response)
            return response

        self._install(ClientHandler, "__call__", call)
        self._install(AsyncClientHandler, "__call__", async_call)
        self._freeze()

    def started(self, sender: type[object], **kwargs: object) -> None:
        if isinstance(sender, type) and issubclass(
            sender, (ClientHandler, AsyncClientHandler)
        ):
            self.stats.client_requests += 1
            carrier = kwargs.get("environ", kwargs.get("scope"))
            if isinstance(carrier, dict):
                receipt = next(self.receipts)
                carrier[RECEIPT_KEY] = receipt
                self.started_receipts[receipt] += 1

    def evaluated(self, request: HttpRequest) -> None:
        self.stats.evaluated_requests += 1
        carrier = getattr(request, "scope", request.META)
        receipt = carrier.get(RECEIPT_KEY)
        if isinstance(receipt, int):
            self.evaluated_receipts[receipt] += 1

    def accounting_errors(self) -> list[str]:
        # Receipts are issued in order and tests are checked after their
        # requests complete, so each receipt is accounted exactly once.
        errors = []
        seen = self.started_receipts.keys() | self.evaluated_receipts.keys()
        latest = max(seen, default=self.accounted_through)
        for receipt in sorted(
            seen | set(range(self.accounted_through + 1, latest + 1))
        ):
            started = self.started_receipts.pop(receipt, 0)
            evaluated = self.evaluated_receipts.pop(receipt, 0)
            if started != 1 or evaluated != 1:
                errors.append(
                    "Client request was not evaluated exactly once: "
                    f"started={started} evaluated={evaluated}"
                )
        self.accounted_through = max(self.accounted_through, latest)
        return errors

    def require_receipt(self, response: HttpResponseBase) -> None:
        if getattr(response, OBSERVED_ATTRIBUTE, None) is not self:
            self.tampering.append("Client response bypassed refusal middleware")
            pytest.fail(self.tampering[-1])

    def integrity_errors(self) -> list[str]:
        started = perf_counter_ns()
        errors = []
        for (owner, name), expected in self.expected.items():
            if getattr(owner, name) is not expected:
                errors.append(f"Refusal observer replaced: {owner.__name__}.{name}")
        for label, (holder, name, expected) in self.dependencies.items():
            if getattr(holder, name) is not expected:
                errors.append(f"Refusal guard dependency replaced: {label}")
        for state in self.function_states:
            errors.extend(
                f"Refusal guard function rewritten: "
                f"{state.function.__qualname__}.{part}"
                for part in state.changed()
            )
        errors.extend(self.accounting_errors())
        receivers, _ = signals.request_started._live_receivers(ClientHandler)
        if self.started not in receivers:
            errors.append("Independent client observer disconnected")
        errors.extend(self._chain_errors())
        self.stats.integrity_ns += perf_counter_ns() - started
        return errors

    def _chain_errors(self) -> list[str]:
        errors = []
        for handler, (middleware_ref, chain_ref) in tuple(self.chains.items()):
            middleware = middleware_ref()
            if (
                cast("_HandlerChain", handler)._middleware_chain is not middleware
                or middleware is None
            ):
                errors.append("Outermost refusal middleware removed")
            elif getattr(middleware, "get_response", None) is not chain_ref():
                errors.append("Refusal middleware response chain replaced")
            elif getattr(middleware, "observer", None) is not self:
                errors.append("Refusal middleware observer replaced")
            elif getattr(middleware, "check", None) is not self.check:
                errors.append("Refusal middleware check replaced")
        return errors

    def receipt(self, errors: list[str], *, gate_selected: bool) -> dict[str, object]:
        """What an xdist worker sends its controller at session finish.

        A worker sees only its share of the collection, and xdist drops its
        session exit status, so session-level checks over the totals (and the
        exit gate over every worker's executed exits) run in the controller.
        """
        return {
            "errors": errors,
            "stats": asdict(self.stats),
            "seen": sorted(site.location for site in self.trace.seen),
            "gate": gate_selected,
        }

    def merge(self, receipts: list[object]) -> list[str]:
        """xdist controller: fold every worker's receipt into this session.

        Fails closed on a worker without a receipt. When the exit gate was
        selected, every derived refusal exit must have run in some worker.
        """
        errors: list[str] = []
        if not receipts:
            errors.append("Distributed session returned no refusal guard receipt")
        seen: set[str] = set()
        gate = False
        for receipt in receipts:
            if not isinstance(receipt, dict):
                errors.append(
                    "An xdist worker finished without a refusal guard receipt"
                )
                continue
            errors.extend(receipt["errors"])
            for field, value in receipt["stats"].items():
                setattr(self.stats, field, getattr(self.stats, field) + value)
            seen.update(receipt["seen"])
            gate = gate or bool(receipt["gate"])
        if gate:
            missing = sorted(
                site.location for site in self.trace.exits if site.location not in seen
            )
            if missing:
                errors.append("Unreached refusal exits:\n" + "\n".join(missing))
        return errors

    def session_errors(self, *, totals: bool = True) -> list[str]:
        """Tampering and integrity; with ``totals``, also the count checks.

        An xdist worker passes ``totals=False``: its controller checks the
        summed counts once every worker's receipt has arrived.
        """
        errors = [*self.tampering, *self.integrity_errors()]
        if not totals:
            return errors
        if self.stats.violations:
            errors.append(f"Refusal session violations: {self.stats.violations}")
        if self.stats.client_requests != self.stats.evaluated_requests:
            errors.append(
                "Client request count differs from evaluated responses: "
                f"requests={self.stats.client_requests} "
                f"evaluated={self.stats.evaluated_requests}"
            )
        if self.stats.client_requests:
            if not self.stats.responses:
                errors.append(
                    "Django client ran but refusal observer saw zero responses"
                )
            if not self.stats.refusals:
                errors.append(
                    "Django client ran but refusal observer saw zero refusals"
                )
        return errors

    def stop(self) -> None:
        self.trace.stop()
        signals.request_started.disconnect(dispatch_uid=STARTED_UID)
        self.patch.undo()

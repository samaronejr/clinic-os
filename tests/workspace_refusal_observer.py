"""Outermost test-handler observation, independent receipts and tamper checks."""

from __future__ import annotations

import sys
from collections import Counter
from functools import wraps
from itertools import count
from time import perf_counter_ns
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
    from collections.abc import Awaitable, Callable

    from django.core.handlers.base import BaseHandler
    from django.http import HttpRequest, HttpResponseBase

    from workspace_refusal_support import RefusalCheck

    type ResponseHandler = (
        Callable[[HttpRequest], HttpResponseBase]
        | Callable[[HttpRequest], Awaitable[HttpResponseBase]]
    )


class _HandlerChain(Protocol):
    _middleware_chain: object


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
        self.expected: dict[tuple[type[object], str], object] = {}
        self.chains: WeakKeyDictionary[BaseHandler, tuple[ref[object], ref[object]]] = (
            WeakKeyDictionary()
        )
        self.tampering: list[str] = []
        self.receipts = count(1)
        self.started_receipts: Counter[int] = Counter()
        self.evaluated_receipts: Counter[int] = Counter()
        self.accounted_through = 0
        self.check: RefusalCheck | None = None
        self.dependencies: dict[str, tuple[object, str, object]] = {}

    def _install(self, owner: type[object], name: str, value: object) -> None:
        self.patch.setattr(owner, name, value)
        self.expected[owner, name] = value

    def _bind_dependencies(self) -> RefusalCheck:
        check, self.dependencies = bind_refusal_check()
        self.check = check
        module = sys.modules[__name__]
        for name, value in vars(module).items():
            if getattr(value, "__module__", None) == check_refusal.__module__:
                self.dependencies[f"{__name__}.{name}"] = (module, name, value)
        # Every observer method and every guard-owned middleware method is a
        # dependency too: a later class patch cannot silently alter the path.
        for owner in (RefusalMiddleware, RefusalObserver, RefusalTrace):
            for name, value in vars(owner).items():
                if callable(value) and (
                    not name.startswith("__") or name in {"__init__", "__call__"}
                ):
                    self.expected[owner, name] = value
        self.expected[MiddlewareMixin, "__call__"] = MiddlewareMixin.__call__
        return check

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

    def session_errors(self) -> list[str]:
        errors = [*self.tampering, *self.integrity_errors()]
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

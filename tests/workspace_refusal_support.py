"""Shared staff-workspace route discovery and collection-wide refusal oracle."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from time import perf_counter_ns
from types import FunctionType
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

import pytest
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY, AUTH_FLOW_VIEWS
from apps.tenancy.middleware import BYPASS_PATHS, BYPASS_PREFIXES, PATIENT_PREFIX
from django.conf import settings
from django.urls import Resolver404, URLPattern, URLResolver, resolve, reverse
from django.urls.converters import IntConverter, StringConverter, UUIDConverter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.http import HttpRequest, HttpResponseBase


class RefusalCheck(Protocol):
    def __call__(
        self,
        request: HttpRequest,
        response: HttpResponseBase,
        stats: RefusalGuardStats,
    ) -> None: ...


@dataclass(slots=True)
class RefusalGuardStats:
    client_requests: int = 0
    evaluated_requests: int = 0
    integrity_ns: int = 0
    trace_ns: int = 0
    responses: int = 0
    refusals: int = 0
    violations: int = 0
    elapsed_ns: int = 0


GUARD_STATS: pytest.StashKey[RefusalGuardStats] = pytest.StashKey()


def workspace_scope(path: str, view_name: str | None) -> bool:
    """Use the production staff/patient/public/auth boundaries, not a route list."""
    return not (
        path in BYPASS_PATHS
        or path.startswith((*BYPASS_PREFIXES, PATIENT_PREFIX))
        or view_name in AUTH_FLOW_VIEWS
    )


def check_refusal(
    request: HttpRequest, response: HttpResponseBase, stats: RefusalGuardStats
) -> None:
    """Fail outside Django's exception handler; never print cookies or payloads."""
    started = perf_counter_ns()
    stats.responses += 1
    try:
        if response.status_code not in {403, 404}:
            return
        match = request.resolver_match
        if match is None:
            try:
                match = resolve(
                    request.path_info, urlconf=getattr(request, "urlconf", None)
                )
            except Resolver404:
                match = None
        name = match.view_name if match is not None else None
        if not workspace_scope(request.path_info, name):
            return
        session = getattr(request, "session", None)
        clinic_parameter = match is not None and "clinic_id" in match.kwargs
        remembered_clinic = session is not None and ACTIVE_CLINIC_SESSION_KEY in session
        if not clinic_parameter and not remembered_clinic:
            # Authentication primitive probes need not be clinic-scoped. Cover
            # every deployed staff route (including fresh, session-scoped ones),
            # plus test/extension routes declaring or remembering a clinic.
            try:
                resolve(request.path_info, urlconf="config.urls")
            except Resolver404:
                return
        stats.refusals += 1
        modified = session is not None and session.modified
        cookie = settings.SESSION_COOKIE_NAME in response.cookies
        if modified or cookie:
            stats.violations += 1
            pytest.fail(
                f"Workspace refusal wrote session: view={name!r} "
                f"method={request.method} status={response.status_code} "
                f"session.modified={modified} session_cookie={cookie}",
                pytrace=True,
            )
    finally:
        stats.elapsed_ns += perf_counter_ns() - started


def _function(function: object) -> FunctionType:
    assert isinstance(function, FunctionType)
    return function


def _function_globals(function: object) -> frozenset[str]:
    module = vars(sys.modules[__name__])
    code = _function(function).__code__
    return frozenset(name for name in code.co_names if name in module)


def bind_refusal_check() -> tuple[RefusalCheck, dict[str, tuple[object, str, object]]]:
    """Freeze the guard's code and every module global it reads at install time."""
    module = sys.modules[__name__]
    names = _function_globals(check_refusal) | _function_globals(workspace_scope)
    namespace: dict[str, object] = {"__builtins__": __builtins__}
    namespace.update((name, getattr(module, name)) for name in names)
    scope = FunctionType(_function(workspace_scope).__code__, namespace)
    namespace["workspace_scope"] = scope
    check = cast(
        "RefusalCheck", FunctionType(_function(check_refusal).__code__, namespace)
    )
    dependencies: dict[str, tuple[object, str, object]] = {
        f"{__name__}.{name}": (module, name, getattr(module, name))
        for name in sorted(names | {"check_refusal", "workspace_scope"})
    }
    # The frozen namespace keeps the pytest module; also bind the method called.
    dependencies["pytest.fail"] = (pytest, "fail", pytest.fail)
    return check, dependencies


@dataclass(frozen=True)
class WorkspaceRoute:
    name: str
    kwargs: dict[str, object]
    pattern: URLPattern

    def url(self, clinic_id: UUID) -> str:
        kwargs = dict(self.kwargs)
        if "clinic_id" in kwargs:
            kwargs["clinic_id"] = clinic_id
        return reverse(self.name, kwargs=kwargs)


def _sample(converter: object) -> object:
    if isinstance(converter, UUIDConverter):
        return uuid4()
    if isinstance(converter, IntConverter):
        return 1
    if isinstance(converter, StringConverter):
        return "synthetic"
    pytest.fail(f"Uninspected URL converter: {type(converter).__name__}")


def workspace_routes(
    resolver: URLResolver,
    namespace: str = "",
    inherited: dict[str, object] | None = None,
) -> Iterator[WorkspaceRoute]:
    """Discover session-scoped as well as URL-scoped staff routes recursively."""
    for entry in resolver.url_patterns:
        converters = {**(inherited or {}), **entry.pattern.converters}
        if isinstance(entry, URLResolver):
            prefix = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from workspace_routes(entry, prefix, converters)
        else:
            assert isinstance(entry, URLPattern)
            assert entry.name is not None, "Staff routes must be named and reversible"
            parameters = entry.pattern.regex.groupindex.keys() | converters.keys()
            assert parameters <= converters.keys(), "Uninspected regex route parameters"
            route = WorkspaceRoute(
                f"{namespace}{entry.name}",
                {key: _sample(value) for key, value in converters.items()},
                entry,
            )
            if workspace_scope(route.url(uuid4()), route.name):
                yield route

"""Mechanically derived refusal exits, observed on real request execution.

PEP 669 local line events coexist with pytest-cov rather than replacing its
tracer. Only code in the resolver-derived view modules is instrumented; an exit
is credited only on a request which actually returns a 4xx response.
"""

from __future__ import annotations

import ast
import inspect
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns
from types import CodeType, FunctionType, ModuleType
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseBase
from django.shortcuts import render
from django.urls import get_resolver

from workspace_refusal_support import workspace_routes

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True, order=True)
class RefusalExit:
    file: str
    line: int
    kind: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line} ({self.kind})"


def _value(node: ast.expr, namespace: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_value(node.value, namespace), node.attr, None)
    return None


def view_modules() -> tuple[ModuleType, ...]:
    modules: dict[str, ModuleType] = {}
    for route in workspace_routes(get_resolver()):
        callback = route.pattern.callback
        target = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        module = inspect.getmodule(target or inspect.unwrap(callback))
        assert module is not None
        assert module.__file__ is not None
        assert module.__name__.startswith("apps."), module.__name__
        modules[module.__name__] = module
    return tuple(modules[name] for name in sorted(modules))


def _refusal_raise(node: ast.Raise, namespace: dict[str, object]) -> bool:
    if node.exc is None:
        return False
    expression = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    exception = _value(expression, namespace)
    name = getattr(exception, "__name__", "")
    return (
        (
            isinstance(exception, type)
            and issubclass(exception, (Http404, PermissionDenied))
        )
        or name.endswith("AccessDeniedError")
        or name == "UiApiError"
    )


def _response_factory(target: object) -> bool:
    if target is render:
        return True
    if isinstance(target, type):
        return issubclass(target, HttpResponseBase)
    if isinstance(target, FunctionType):
        return "Response" in str(inspect.signature(target).return_annotation)
    return False


def _refusal_call(node: ast.Call, namespace: dict[str, object]) -> str | None:
    target = _value(node.func, namespace)
    if getattr(target, "__name__", "") == "_denied":
        return "_denied"
    for keyword in node.keywords:
        if keyword.arg == "status" and _response_factory(target):
            status = _value(keyword.value, namespace)
            # A dynamic status is not assumed harmless: execute it under 4xx.
            if status is None or (isinstance(status, int) and 400 <= status < 500):
                return "4xx-response"
    if isinstance(target, type):
        status = getattr(target, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return "4xx-response"
    return None


def refusal_exits(module: ModuleType) -> frozenset[RefusalExit]:
    assert module.__file__ is not None
    path = Path(module.__file__).resolve()
    namespace = vars(module)
    tree = ast.parse(path.read_text(), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Raise, ast.Call)):
            continue
        kind = (
            ("raise" if _refusal_raise(node, namespace) else None)
            if isinstance(node, ast.Raise)
            else _refusal_call(node, namespace)
        )
        if kind is not None:
            found.add(RefusalExit(str(path), node.lineno, kind))
    return frozenset(found)


def _codes(value: object, filename: str) -> Iterator[CodeType]:
    if isinstance(value, (classmethod, staticmethod)):
        value = value.__func__
    if isinstance(value, FunctionType):
        unwrapped = inspect.unwrap(value)
        if isinstance(unwrapped, FunctionType):
            yield from _nested_codes(unwrapped.__code__, filename)
    elif isinstance(value, type) and value.__module__.startswith("apps."):
        for member in vars(value).values():
            if not isinstance(member, type):
                yield from _codes(member, filename)


def _nested_codes(code: CodeType, filename: str) -> Iterator[CodeType]:
    if code.co_filename == filename:
        yield code
        for constant in code.co_consts:
            if isinstance(constant, CodeType):
                yield from _nested_codes(constant, filename)


class RefusalTrace:
    def __init__(self) -> None:
        self.modules = view_modules()
        self.exits = frozenset(
            site for module in self.modules for site in refusal_exits(module)
        )
        assert self.exits, "No refusal exits discovered"
        self.by_line: dict[tuple[str, int], set[RefusalExit]] = {}
        for site in self.exits:
            self.by_line.setdefault((site.file, site.line), set()).add(site)
        self.seen: set[RefusalExit] = set()
        self.requests: ContextVar[tuple[set[RefusalExit], ...]] = ContextVar(
            "refusal-exit-requests", default=()
        )
        self.tool = 3
        self.codes: set[CodeType] = set()
        self.elapsed_ns = 0

    def start(self) -> None:
        sys.monitoring.use_tool_id(self.tool, "workspace-refusal-exits")
        sys.monitoring.register_callback(
            self.tool, sys.monitoring.events.LINE, self.line
        )
        for module in self.modules:
            assert module.__file__ is not None
            filename = str(Path(module.__file__).resolve())
            for value in tuple(vars(module).values()):
                self.codes.update(
                    code
                    for code in _codes(value, filename)
                    if any(
                        (filename, line) in self.by_line
                        for _, _, line in code.co_lines()
                    )
                )
        for code in self.codes:
            sys.monitoring.set_local_events(self.tool, code, sys.monitoring.events.LINE)

    def line(self, code: CodeType, line: int) -> object:
        started = perf_counter_ns()
        try:
            sites = self.by_line.get((code.co_filename, line))
            if sites is None:
                # PEP 669 disables this individual location for subsequent calls.
                return sys.monitoring.DISABLE
            requests = self.requests.get()
            if requests:
                requests[-1].update(sites)
            return None
        finally:
            self.elapsed_ns += perf_counter_ns() - started

    def begin_request(self) -> None:
        self.requests.set((*self.requests.get(), set()))

    def finish_request(self, status: int) -> None:
        requests = self.requests.get()
        assert requests
        self.requests.set(requests[:-1])
        if 400 <= status < 500:
            self.seen.update(requests[-1])

    def missing(self) -> list[str]:
        return [site.location for site in sorted(self.exits - self.seen)]

    def stop(self) -> None:
        for code in self.codes:
            sys.monitoring.set_local_events(self.tool, code, 0)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(self.tool)

"""Derive workflow authority reachability without trusting inventory labels."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, is_dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

from apps.identity.current_context import require_permission
from apps.workflows import services
from django.urls import URLPattern, URLResolver, get_resolver

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class GuardFunction:
    symbol: str
    function: Callable[..., object]
    calls: frozenset[str]


def symbol(function: Callable[..., object]) -> str:
    plain = inspect.unwrap(function)
    return f"{plain.__module__}.{plain.__qualname__}"


def _resolve(node: ast.AST, module: ModuleType) -> object:
    if isinstance(node, ast.Name):
        return vars(module).get(node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_resolve(node.value, module), node.attr, None)
    return None


def _nodes(
    nodes: list[ast.stmt], prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef]]:
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef):
            yield from _nodes(node.body, prefix + node.name + ".")


def _routes(resolver: URLResolver) -> Iterator[Callable[..., object]]:
    for route in resolver.url_patterns:
        if isinstance(route, URLResolver):
            yield from _routes(route)
        elif isinstance(route, URLPattern) and route.callback.__module__.startswith(
            "apps.workflows."
        ):
            yield route.callback


def public_boundaries() -> set[str]:
    exports = set()
    for name in services.__all__:
        function = getattr(services, name)
        if inspect.isfunction(function):
            exports.add(symbol(function))
        else:
            assert isinstance(function, type)
            assert issubclass(function, Exception) or (
                is_dataclass(function) and "__call__" not in vars(function)
            ), (name, "uninspectable service export")
    return exports | {symbol(function) for function in _routes(get_resolver())}


def discover_guards() -> dict[str, GuardFunction]:
    definitions = {}
    for path in sorted((ROOT / "apps/workflows").glob("*.py")):
        module = import_module("apps.workflows." + path.stem)
        for name, node in _nodes(ast.parse(path.read_text()).body):
            value: object = module
            for part in name.split("."):
                value = getattr(value, part)
            assert inspect.isfunction(value), (path, name, "opaque callable")
            calls = set()
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    target = _resolve(call.func, module)
                    if inspect.isfunction(target):
                        calls.add(symbol(target))
            key = symbol(value)
            definitions[key] = GuardFunction(key, value, frozenset(calls))
    authority = symbol(require_permission)
    guarded = {name for name, entry in definitions.items() if authority in entry.calls}
    while (
        expanded := {
            name for name, entry in definitions.items() if entry.calls & guarded
        }
        - guarded
    ):
        guarded.update(expanded)
    assert public_boundaries() <= guarded, (
        "public boundary lost permission delegation",
        public_boundaries() - guarded,
    )
    return {name: definitions[name] for name in sorted(guarded)}

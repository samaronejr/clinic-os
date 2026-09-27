"""Finite values for RunSQL: no imports or repository functions are executed."""

from __future__ import annotations

import ast
import operator
from collections.abc import Container
from typing import TYPE_CHECKING, cast

from psycopg import sql

from .clock_static_scope import UNKNOWN, External, Function, Module, Scope

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import LiteralString


def _attribute(node: ast.Attribute, scope: Scope) -> object:
    value = evaluate(node.value, scope)
    if isinstance(value, Module):
        return value.scope.get(node.attr)
    if isinstance(value, External):
        return External(value.name + "." + node.attr)
    return UNKNOWN


def _container(node: ast.AST, scope: Scope) -> object:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[object] = []
        for item in node.elts:
            value = evaluate(
                item.value if isinstance(item, ast.Starred) else item, scope
            )
            if isinstance(item, ast.Starred) and isinstance(
                value, (tuple, list, set, frozenset)
            ):
                values.extend(value)
            else:
                values.append(value)
        if any(item is UNKNOWN for item in values):
            return UNKNOWN
        return (
            set(values)
            if isinstance(node, ast.Set)
            else tuple(values)
            if isinstance(node, ast.Tuple)
            else values
        )
    if isinstance(node, ast.Dict):
        pairs = [
            (
                evaluate(key, scope) if key is not None else UNKNOWN,
                evaluate(value, scope),
            )
            for key, value in zip(node.keys, node.values, strict=True)
        ]
        return (
            UNKNOWN
            if any(key is UNKNOWN or value is UNKNOWN for key, value in pairs)
            else dict(pairs)
        )
    if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        scopes = comprehension_scopes(node.generators, scope)
        values = (
            [] if scopes is None else [evaluate(node.elt, child) for child in scopes]
        )
        return (
            UNKNOWN
            if scopes is None or any(value is UNKNOWN for value in values)
            else values
        )
    return UNKNOWN


def comprehension_scopes(
    generators: list[ast.comprehension], scope: Scope
) -> list[Scope] | None:
    scopes = [scope]
    for generator in generators:
        expanded = []
        for parent in scopes:
            values = evaluate(generator.iter, parent)
            if not isinstance(values, (tuple, list, set, frozenset, dict)):
                return None
            for value in values:
                child = Scope(parent.module, parent)
                child.bind(generator.target, value)
                conditions = [evaluate(condition, child) for condition in generator.ifs]
                if any(condition is UNKNOWN for condition in conditions):
                    return None
                if all(conditions):
                    expanded.append(child)
        scopes = expanded
    return scopes


BINARY: dict[type[ast.operator], Callable[..., object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Mod: operator.mod,
    ast.BitOr: operator.or_,
}
COMPARE: dict[type[ast.cmpop], Callable[[object, object], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def _comparison(node: ast.Compare, scope: Scope) -> object:
    left, right = evaluate(node.left, scope), evaluate(node.comparators[0], scope)
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    if isinstance(node.ops[0], (ast.In, ast.NotIn)):
        if not isinstance(right, Container):
            return UNKNOWN
        contained = operator.contains(cast("Container[object]", right), left)
        return not contained if isinstance(node.ops[0], ast.NotIn) else contained
    compare = COMPARE.get(type(node.ops[0]))
    return UNKNOWN if compare is None else compare(left, right)


def _operation(node: ast.AST, scope: Scope) -> object:
    if isinstance(node, ast.BinOp):
        left, right = evaluate(node.left, scope), evaluate(node.right, scope)
        operation = BINARY.get(type(node.op))
        return (
            UNKNOWN
            if left is UNKNOWN or right is UNKNOWN or operation is None
            else operation(left, right)
        )
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return _comparison(node, scope)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = evaluate(node.operand, scope)
        return UNKNOWN if value is UNKNOWN else not value
    if isinstance(node, ast.IfExp):
        condition = evaluate(node.test, scope)
        return (
            UNKNOWN
            if condition is UNKNOWN
            else evaluate(node.body if condition else node.orelse, scope)
        )
    return UNKNOWN


def evaluate(node: ast.AST, scope: Scope) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return scope.get(node.id)
    if isinstance(node, ast.Attribute):
        return _attribute(node, scope)
    if isinstance(node, ast.Call):
        return _call(node, scope)
    return _compound(node, scope)


def _formatted(node: ast.FormattedValue, scope: Scope) -> object:
    value = evaluate(node.value, scope)
    if not isinstance(value, (str, int, float, bool, type(None))):
        return UNKNOWN
    if node.conversion == ord("r"):
        value = repr(value)
    elif node.conversion == ord("a"):
        value = ascii(value)
    elif node.conversion == ord("s"):
        value = str(value)
    spec = evaluate(node.format_spec, scope) if node.format_spec is not None else ""
    return format(value, spec) if isinstance(spec, str) else UNKNOWN


def _compound(node: ast.AST, scope: Scope) -> object:
    match node:
        case ast.JoinedStr():
            parts = [evaluate(part, scope) for part in node.values]
            return (
                UNKNOWN
                if any(part is UNKNOWN for part in parts)
                else "".join(str(part) for part in parts)
            )
        case ast.FormattedValue():
            return _formatted(node, scope)
        case ast.Slice():
            bounds = [
                evaluate(part, scope) if part is not None else None
                for part in (node.lower, node.upper, node.step)
            ]
            return (
                slice(*bounds)
                if all(value is None or isinstance(value, int) for value in bounds)
                else UNKNOWN
            )
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=int() as number)):
            return -number
        case ast.Subscript():
            return _subscript(evaluate(node.value, scope), evaluate(node.slice, scope))
        case _:
            return (
                _container(node, scope)
                if isinstance(
                    node,
                    (
                        ast.List,
                        ast.Tuple,
                        ast.Set,
                        ast.Dict,
                        ast.ListComp,
                        ast.GeneratorExp,
                        ast.SetComp,
                    ),
                )
                else _operation(node, scope)
            )


def _subscript(value: object, index: object) -> object:
    if not isinstance(value, (str, tuple, list)):
        return UNKNOWN
    if isinstance(index, int):
        return value[index]
    if isinstance(index, slice) and all(
        part is None or isinstance(part, int)
        for part in (index.start, index.stop, index.step)
    ):
        return value[
            slice(
                index.start if isinstance(index.start, int) else None,
                index.stop if isinstance(index.stop, int) else None,
                index.step if isinstance(index.step, int) else None,
            )
        ]
    return UNKNOWN


def _method(
    node: ast.Attribute, args: list[object], kwargs: dict[str, object], scope: Scope
) -> object:
    receiver = evaluate(node.value, scope)
    if isinstance(receiver, str) and node.attr in {
        "join",
        "format",
        "replace",
        "split",
        "strip",
        "lower",
        "upper",
        "index",
    }:
        return getattr(receiver, node.attr)(*args, **kwargs)
    if (
        isinstance(receiver, dict)
        and node.attr in {"items", "keys", "values"}
        and not args
        and not kwargs
    ):
        return list(getattr(receiver, node.attr)())
    if isinstance(receiver, sql.Composable) and node.attr in {"format", "as_string"}:
        return getattr(receiver, node.attr)(*args, **kwargs)
    return UNKNOWN


def _external(
    function: External, args: list[object], kwargs: dict[str, object]
) -> object:
    if not all(isinstance(arg, str) for arg in args) or kwargs:
        return UNKNOWN
    if function.name == "psycopg.sql.SQL" and len(args) == 1:
        # Repository AST-derived template construction only; no SQL is executed.
        return sql.SQL(cast("LiteralString", args[0]))
    if function.name == "psycopg.sql.Identifier":
        return sql.Identifier(*(str(arg) for arg in args))
    return UNKNOWN


def _builtin(name: str, args: list[object], kwargs: dict[str, object]) -> object:
    builtins: dict[str, Callable[..., object]] = {
        "sorted": sorted,
        "reversed": lambda value: list(reversed(value)),
        "tuple": tuple,
        "list": list,
        "set": set,
        "frozenset": frozenset,
        "str": str,
        "len": len,
    }
    return builtins[name](*args, **kwargs) if name in builtins else UNKNOWN


def _call(node: ast.Call, scope: Scope) -> object:
    args = [evaluate(arg, scope) for arg in node.args]
    kwargs = {
        kw.arg: evaluate(kw.value, scope) for kw in node.keywords if kw.arg is not None
    }
    if any(arg is UNKNOWN for arg in args + list(kwargs.values())) or any(
        kw.arg is None for kw in node.keywords
    ):
        return UNKNOWN
    function = evaluate(node.func, scope)
    if isinstance(function, Function):
        return _function(function, args, kwargs)
    if isinstance(function, External):
        return _external(function, args, kwargs)
    if isinstance(node.func, ast.Attribute):
        return _method(node.func, args, kwargs, scope)
    if isinstance(node.func, ast.Name) and not scope.contains(node.func.id):
        return _builtin(node.func.id, args, kwargs)
    return UNKNOWN


def _function(
    function: Function, args: list[object], kwargs: dict[str, object]
) -> object:
    if function.node.decorator_list:
        return UNKNOWN
    scope = Scope(function.scope.module, function.scope)
    parameters = function.node.args.args
    names = {parameter.arg for parameter in parameters + function.node.args.kwonlyargs}
    if len(args) > len(parameters) or not set(kwargs) <= names:
        return UNKNOWN
    for parameter, default in zip(
        parameters[-len(function.node.args.defaults) :],
        function.node.args.defaults,
        strict=False,
    ):
        scope.values[parameter.arg] = evaluate(default, function.scope)
    for parameter, keyword_default in zip(
        function.node.args.kwonlyargs, function.node.args.kw_defaults, strict=True
    ):
        if keyword_default is not None:
            scope.values[parameter.arg] = evaluate(keyword_default, function.scope)
    scope.values.update(
        {
            parameter.arg: value
            for parameter, value in zip(parameters, args, strict=False)
        }
    )
    scope.values.update(kwargs)
    return scope.module.modules.statements(function.node.body, scope)[1]

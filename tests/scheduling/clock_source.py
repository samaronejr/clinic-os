"""Decode Python SQL literals before enforcing the unsupported-SQL boundary."""

from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache
from string import Formatter

DYNAMIC = "__clock_unresolved__"


@lru_cache(maxsize=2048)
def parsed_source(source: str) -> ast.Module:
    return ast.parse(source)


def _text(node: ast.AST, values: dict[str, str]) -> str:
    if isinstance(node, ast.FormattedValue):
        return _text(node.value, values)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_text(value, values) for value in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left, values) + _text(node.right, values)
    if isinstance(node, ast.Call):
        formatted = isinstance(node.func, ast.Attribute) and node.func.attr == "format"
        return (
            "".join(
                literal + (DYNAMIC if field is not None else "")
                for literal, field, _, _ in Formatter().parse(
                    _text(node.func.value, values)
                    if isinstance(node.func, ast.Attribute)
                    else DYNAMIC
                )
            )
            if formatted
            else values.get(ast.unparse(node.func) + "()", DYNAMIC)
        )
    return values.get(node.id, DYNAMIC) if isinstance(node, ast.Name) else DYNAMIC


def _assignments(tree: ast.AST) -> dict[str, ast.AST]:
    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = (
                    node.value if target.id not in assignments else ast.Constant(None)
                )
    return assignments


def _loop_rows(loop: ast.For, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    if not isinstance(loop.iter, ast.Name):
        return loop.iter
    root: ast.AST = loop
    while root in parents:
        root = parents[root]
    return _assignments(root).get(loop.iter.id, loop.iter)


def _names(
    argument: ast.AST, call: ast.AST, parents: dict[ast.AST, ast.AST]
) -> list[str]:
    """Resolve only literal bindings, including finite literal-pair loops."""
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return [argument.value]
    if not isinstance(argument, ast.Name):
        return []
    parent = parents.get(call)
    while parent is not None:
        if (
            isinstance(parent, ast.For)
            and isinstance(parent.target, ast.Tuple)
            and isinstance(rows := _loop_rows(parent, parents), ast.Tuple)
        ):
            positions = [
                i
                for i, target in enumerate(parent.target.elts)
                if isinstance(target, ast.Name) and target.id == argument.id
            ]
            if positions:
                values = []
                for row in rows.elts:
                    if not isinstance(row, ast.Tuple) or len(row.elts) <= positions[0]:
                        return []
                    value = row.elts[positions[0]]
                    if not isinstance(value, ast.Constant) or not isinstance(
                        value.value, str
                    ):
                        return []
                    values.append(value.value)
                return values
        parent = parents.get(parent)
    return []


def _framework_query(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(call)
    while parent is not None:
        if (
            isinstance(parent, ast.For)
            and isinstance(parent.target, ast.Name)
            and isinstance(call.args[0], ast.Name)
            and parent.target.id == call.args[0].id
        ):
            return ast.unparse(parent.iter) == "schema_editor.deferred_sql"
        parent = parents.get(parent)
    return False


def _bound_query(
    call: ast.Call, parents: dict[ast.AST, ast.AST], values: dict[str, str]
) -> list[str]:
    source = _text(call.args[0], values)
    if source.lstrip().startswith(DYNAMIC):
        finite_queries = _names(call.args[0], call, parents)
        if finite_queries:
            return finite_queries
        return [
            "__clock_django_deferred_sql__"
            if _framework_query(call, parents)
            else "__clock_unresolved_statement__"
        ]
    if (
        len(call.args) < 2
        or not isinstance(call.args[1], (ast.List, ast.Tuple))
        or not call.args[1].elts
    ):
        return [source]
    names = _names(call.args[1].elts[0], call, parents)
    if not names:
        return [source]
    return [
        source.replace("%s", "'" + name.replace("'", "''") + "'", 1) for name in names
    ]


def _contexts(tree: ast.Module) -> dict[ast.AST, list[dict[str, str]]]:
    """Expand finite literal arguments of local SQL-template builders."""
    constants = {
        name: value.value
        for name, value in _assignments(tree).items()
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    nodes = tuple(ast.walk(tree))
    local_calls: dict[str, list[ast.Call]] = defaultdict(list)
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            local_calls[node.func.id].append(node)
    for function in nodes:
        if isinstance(function, ast.FunctionDef):
            returns = [
                node.value
                for node in ast.walk(function)
                if isinstance(node, ast.Return) and node.value is not None
            ]
            if len(returns) == 1:
                constants[function.name + "()"] = _text(returns[0], constants)
    contexts = {node: [constants] for node in nodes}
    for function in nodes:
        if not isinstance(function, ast.FunctionDef):
            continue
        calls = local_calls[function.name]
        bindings = [
            constants
            | {
                parameter.arg: _text(argument, constants)
                for parameter, argument in zip(
                    function.args.args, call.args, strict=False
                )
            }
            for call in calls
        ]
        if bindings:
            for node in ast.walk(function):
                contexts[node] = bindings
    return contexts


def _ordinary_fragments(node: ast.AST, contexts: list[dict[str, str]]) -> list[str]:
    if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        return [_text(node, values) for values in contexts]
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in {
            "eval",
            "exec",
            "compile",
        }:
            return ["__clock_runtime_code__"]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return [_text(node, values) for values in contexts]
    return []


def source_fragments(tree: ast.Module) -> list[str]:
    return list(_cached_fragments(tree))


@lru_cache(maxsize=2048)
def _cached_fragments(tree: ast.Module) -> tuple[str, ...]:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    contexts = _contexts(tree)
    skipped: set[ast.AST] = set()
    result = []
    for node in ast.walk(tree):
        if (
            isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            skipped.add(node.body[0].value)  # A docstring is not executable SQL.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany"}
            and node.args
        ):
            for values in contexts[node]:
                result.extend(_bound_query(node, parents, values))
            skipped.update(ast.walk(node.args[0]))
    return tuple(
        result
        + [
            fragment
            for node in ast.walk(tree)
            if node not in skipped
            for fragment in _ordinary_fragments(node, contexts[node])
        ]
    )

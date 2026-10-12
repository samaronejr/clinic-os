"""Finite statement execution over decoder values, never repository code."""

from __future__ import annotations

import ast

from .clock_static_scope import UNKNOWN, Scope
from .clock_static_values import evaluate


def statements(body: list[ast.stmt], scope: Scope) -> tuple[bool, object]:
    for node in body:
        returned, value = statement(node, scope)
        if returned:
            return True, value
    return False, None


def _loop(node: ast.For, scope: Scope) -> tuple[bool, object]:
    values = evaluate(node.iter, scope)
    if not isinstance(values, (tuple, list, set, frozenset, dict)):
        return True, UNKNOWN
    for item in values:
        scope.bind(node.target, item)
        returned, value = statements(node.body, scope)
        if returned:
            return True, value
    return statements(node.orelse, scope)


def _expression(node: ast.Expr, scope: Scope) -> tuple[bool, object]:
    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
        return False, None
    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
        receiver = evaluate(node.value.func.value, scope)
        args = [evaluate(arg, scope) for arg in node.value.args]
        if (
            isinstance(receiver, list)
            and node.value.func.attr in {"append", "extend"}
            and len(args) == 1
            and args[0] is not UNKNOWN
            and not node.value.keywords
        ):
            getattr(receiver, node.value.func.attr)(args[0])
            return False, None
    return True, UNKNOWN


def _assignment(
    node: ast.Assign | ast.AnnAssign | ast.AugAssign, scope: Scope
) -> tuple[bool, object]:
    if isinstance(node, ast.AugAssign):
        value = evaluate(
            ast.BinOp(left=node.target, op=node.op, right=node.value), scope
        )
        targets: list[ast.expr] = [node.target]
    elif node.value is not None:
        value = evaluate(node.value, scope)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    else:
        return True, UNKNOWN
    if value is UNKNOWN:
        return True, UNKNOWN
    for target in targets:
        scope.bind(target, value)
    return False, None


def statement(node: ast.stmt, scope: Scope) -> tuple[bool, object]:
    if isinstance(node, ast.Return):
        result = (True, evaluate(node.value, scope) if node.value is not None else None)
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        result = _assignment(node, scope)
    elif isinstance(node, ast.If):
        condition = evaluate(node.test, scope)
        result = (
            (True, UNKNOWN)
            if condition is UNKNOWN
            else statements(node.body if condition else node.orelse, scope)
        )
    elif isinstance(node, ast.For):
        result = _loop(node, scope)
    elif isinstance(node, ast.Expr):
        result = _expression(node, scope)
    else:
        result = (True, UNKNOWN)
    return result

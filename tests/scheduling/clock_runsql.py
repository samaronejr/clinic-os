"""Fail closed unless both RunSQL directions decode completely to finite values."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from .clock_static_scope import External, Modules, Scope
from .clock_static_statements import statements
from .clock_static_values import comprehension_scopes, evaluate

if TYPE_CHECKING:
    from pathlib import Path

SENTINEL = "__clock_unresolved_runsql__"


def _scopes(
    node: ast.Call, parents: dict[ast.AST, ast.AST], scope: Scope
) -> list[Scope]:
    ancestors = []
    parent = parents.get(node)
    while parent is not None:
        ancestors.append(parent)
        parent = parents.get(parent)
    scopes = [scope]
    for ancestor in reversed(ancestors):
        if isinstance(ancestor, ast.ClassDef):
            children = []
            for current in scopes:
                child = Scope(current.module, current)
                child.declare(ancestor.body)
                children.append(child)
            scopes = children
        if isinstance(ancestor, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            expanded = [
                comprehension_scopes(ancestor.generators, current) for current in scopes
            ]
            if any(value is None for value in expanded):
                return [scope]
            scopes = [
                child for value in expanded if value is not None for child in value
            ]
    return scopes


def _sql(value: object) -> list[str]:
    if value is None or (
        isinstance(value, External) and value.name.endswith(".RunSQL.noop")
    ):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (tuple, list)):
        result = []
        for item in value:
            if (
                isinstance(item, (tuple, list))
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                result.append(item[0])  # Django's (statement, parameters) form.
            else:
                result.extend(_sql(item))
        return result
    return [SENTINEL]


class SQLDecoder:
    def __init__(self, root: Path) -> None:
        self.modules = Modules(root, evaluate, statements)

    def fragments(self, path: Path, tree: ast.Module) -> list[str]:
        try:
            return self._fragments(path, tree)
        except (TypeError, ValueError, IndexError, RecursionError) as error:
            # A decoding error is an explicit failing source-boundary finding.
            return [SENTINEL + ":" + type(error).__name__]

    def _fragments(self, path: Path, tree: ast.Module) -> list[str]:
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        # No imports execute; external names retain their qualified identity.
        module = self.modules.load(path)
        assert module is not None
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        result = []
        for node in calls:
            named = (
                isinstance(node.func, (ast.Name, ast.Attribute))
                and (
                    node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                )
                == "RunSQL"
            )
            constructor = evaluate(node.func, module.scope)
            if not named and not (
                isinstance(constructor, External)
                and constructor.name.endswith(".RunSQL")
            ):
                continue
            arguments = list(node.args[:2]) + [
                kw.value for kw in node.keywords if kw.arg in {"sql", "reverse_sql"}
            ]
            if not arguments or any(kw.arg is None for kw in node.keywords):
                result.append(SENTINEL)
            for scope in _scopes(node, parents, module.scope):
                for argument in arguments:
                    result.extend(_sql(evaluate(argument, scope)))
        return result

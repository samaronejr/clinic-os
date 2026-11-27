"""Fail closed unless both RunSQL directions decode completely to finite values."""

from __future__ import annotations

import ast
from functools import lru_cache
from typing import TYPE_CHECKING

from django.db import migrations
from django.db.migrations.loader import MigrationLoader

from .clock_static_scope import External, Modules, Scope
from .clock_static_statements import statements
from .clock_static_values import comprehension_scopes, evaluate

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from django.db.migrations.operations.base import Operation

SENTINEL = "__clock_unresolved_runsql__"


def run_sql_operations(operations: Iterable[Operation]) -> Iterator[migrations.RunSQL]:
    for operation in operations:
        if isinstance(operation, migrations.RunSQL):
            yield operation
        elif isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from run_sql_operations(operation.database_operations)


@lru_cache(maxsize=1)
def _graph_constructors() -> frozenset[str]:
    # The graph supplies constructor identities, never SQL values for decoding.
    graph = MigrationLoader(None, replace_migrations=False).graph
    return frozenset(
        f"{constructor.__module__}.{constructor.__name__}"
        for migration in graph.nodes.values()
        for operation in run_sql_operations(migration.operations)
        if (constructor := type(operation)) is not migrations.RunSQL
    )


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

    def bindings(self, path: Path) -> tuple[tuple[str, str], ...]:
        """Resolve module strings with the same finite, import-free decoder."""
        module = self.modules.load(path)
        assert module is not None
        bindings = {
            name: value
            for name in sorted(module.scope.values.keys() | module.scope.expressions)
            if isinstance(value := module.scope.get(name), str)
        }
        parents = {
            child: parent
            for parent in ast.walk(module.tree)
            for child in ast.iter_child_nodes(parent)
        }
        for comprehension in ast.walk(module.tree):
            if not isinstance(
                comprehension, (ast.ListComp, ast.GeneratorExp, ast.SetComp)
            ):
                continue
            for node in ast.walk(comprehension.elt):
                if not isinstance(node, ast.Call):
                    continue
                values = [
                    evaluate(node, scope)
                    for scope in _scopes(node, parents, module.scope)
                ]
                if values and all(isinstance(value, str) for value in values):
                    bindings[f"@{node.lineno}:{node.col_offset}"] = ";\n".join(
                        sorted({str(value) for value in values})
                    )
        return tuple(sorted(bindings.items()))

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
        constructors = _graph_constructors()
        imported_constructors = {
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            for alias in statement.names
            if self._import_identity(path, statement, alias.name) in constructors
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
            imported = (
                isinstance(node.func, ast.Name)
                and node.func.id in imported_constructors
            )
            if not (named or imported) and not (
                isinstance(constructor, External)
                and (
                    constructor.name.endswith(".RunSQL")
                    or constructor.name in constructors
                )
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

    def _import_identity(self, path: Path, statement: ast.ImportFrom, name: str) -> str:
        if statement.level == 0:
            return f"{statement.module}.{name}"
        package = path.relative_to(self.modules.root).parent.parts
        return ".".join(
            part
            for part in (
                *package[: len(package) - statement.level + 1],
                statement.module,
                name,
            )
            if part
        )

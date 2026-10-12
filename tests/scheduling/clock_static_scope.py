"""Repository-local scopes for the finite RunSQL construction decoder."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .clock_source import parsed_source

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

UNKNOWN = object()


@dataclass
class Function:
    node: ast.FunctionDef
    scope: Scope


@dataclass(frozen=True)
class External:
    name: str


class Scope:
    def __init__(self, module: Module, parent: Scope | None = None) -> None:
        self.module = module
        self.parent = parent
        self.values: dict[str, object] = {}
        self.expressions: dict[str, ast.AST] = {}
        self.resolving: set[str] = set()
        self.unresolved: set[str] = set()

    def declare(self, body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        if target.id in self.expressions:
                            self.unresolved.add(target.id)
                        self.expressions[target.id] = node.value
            elif isinstance(node, ast.FunctionDef):
                self.values[node.name] = Function(node, self)
            elif isinstance(
                node, (ast.If, ast.While, ast.Try, ast.TryStar, ast.With, ast.Match)
            ):
                self.unresolved.update(
                    child.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
                )
                self.unresolved.update(
                    child.asname or child.name.split(".")[0]
                    for child in ast.walk(node)
                    if isinstance(child, ast.alias)
                )

    def get(self, name: str) -> object:
        if name in self.unresolved:
            return UNKNOWN
        if name in self.values:
            return self.values[name]
        if name in self.expressions and name not in self.resolving:
            self.resolving.add(name)
            try:
                value = self.module.modules.evaluate(self.expressions[name], self)
            finally:
                self.resolving.remove(name)
            if value is not UNKNOWN:
                self.values[name] = value
            return value
        return self.parent.get(name) if self.parent is not None else UNKNOWN

    def contains(self, name: str) -> bool:
        return (
            name in self.values
            or name in self.unresolved
            or name in self.expressions
            or (self.parent is not None and self.parent.contains(name))
        )

    def bind(self, target: ast.AST, value: object) -> None:
        if isinstance(target, ast.Name):
            self.values[target.id] = value
        elif (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (tuple, list))
            and len(target.elts) == len(value)
        ):
            for part, item in zip(target.elts, value, strict=True):
                self.bind(part, item)


class Module:
    def __init__(self, path: Path, modules: Modules) -> None:
        self.path = path
        self.modules = modules
        self.tree = parsed_source(path.read_text())
        self.scope = Scope(self)
        self.scope.declare(self.tree.body)
        self.imports = [
            node
            for node in self.tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.loaded = False

    def load_imports(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        for node in self.imports:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.scope.values[alias.asname or alias.name.split(".")[0]] = (
                        External(alias.name)
                    )
            else:
                self._load_from(node)
        self._initialise_mutations()

    def _load_from(self, node: ast.ImportFrom) -> None:
        base = self.path.parent if node.level else self.modules.root
        for _ in range(max(0, node.level - 1)):
            base = base.parent
        target = base.joinpath(*(node.module or "").split("."))
        imported = self.modules.load(target)
        for alias in node.names:
            if imported is not None:
                value = imported.scope.get(alias.name)
                if value is UNKNOWN:
                    value = self.modules.load(target / alias.name) or UNKNOWN
            else:
                value = External(".".join(filter(None, (node.module, alias.name))))
            self.scope.values[alias.asname or alias.name] = value

    def _initialise_mutations(self) -> None:
        if not any(
            isinstance(node, (ast.For, ast.AugAssign))
            or (isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant))
            for node in self.tree.body
        ):
            return
        self.scope.expressions.clear()
        body = [
            node
            for node in self.tree.body
            if not isinstance(
                node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)
            )
        ]
        returned, value = self.modules.statements(body, self.scope)
        if returned and value is UNKNOWN:
            # Keep imported constructor identities for alias detection, never
            # decoded SQL values from a module with an unsupported mutation.
            self.scope.values = {
                name: value if isinstance(value, External) else UNKNOWN
                for name, value in self.scope.values.items()
            }


class Modules:
    def __init__(
        self,
        root: Path,
        evaluate: Callable[[ast.AST, Scope], object],
        statements: Callable[[list[ast.stmt], Scope], tuple[bool, object]],
    ) -> None:
        self.root = root
        self.evaluate = evaluate
        self.statements = statements
        self.loaded: dict[Path, Module] = {}

    def load(self, path: Path) -> Module | None:
        path = (
            path
            if path.suffix == ".py"
            else path.with_suffix(".py")
            if path.with_suffix(".py").is_file()
            else path / "__init__.py"
        )
        if not path.is_file():
            return None
        if path not in self.loaded:
            self.loaded[path] = Module(path, self)
        module = self.loaded[path]
        module.load_imports()
        return module

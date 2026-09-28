"""Derive every workflow authorization decision and rewrite it in place.

A decision site is either a direct ``require_permission`` call or a boolean
relation test that reads the current actor: a comparison, a query predicate or
a call to a derived relation predicate. Actor taint is derived by data flow
from ``require_permission``/``current_actor_id`` through assignments, returns
and call arguments, never from spellings. Rewriting swaps a function's code
object, so every alias, decorator closure and URL resolver sees the change.
"""

from __future__ import annotations
import __future__

import ast
import copy
import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)

from workflows.guard_discovery import ROOT, _nodes, _resolve, symbol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import CodeType, ModuleType

RECORDER = "__workflow_decision_site__"
IGNORER = "__workflow_decision_ignore__"
FOREIGN = "__workflow_decision_foreign__"
PERMISSION = "permission"
RELATION = "relation"
SCOPE = "scope"
SCOPE_NAMES = frozenset({"clinic_id", "clinic", "organization_id", "organization"})
QUERY_METHODS = frozenset(
    {"filter", "exclude", "get", "get_or_create", "update_or_create"}
)
WRITE_METHODS = frozenset({"create", "bulk_create", "update"})


@dataclass(frozen=True)
class Site:
    sid: str
    symbol: str
    kind: str
    position: tuple[int, int, int, int]
    detail: str = ""


@dataclass(frozen=True)
class _Function:
    symbol: str
    module: ModuleType
    path: Path
    node: ast.FunctionDef
    function: Callable[..., object]


def _position(node: ast.AST) -> tuple[int, int, int, int]:
    return (
        getattr(node, "lineno", 0),
        getattr(node, "col_offset", 0),
        getattr(node, "end_lineno", 0) or 0,
        getattr(node, "end_col_offset", 0) or 0,
    )


@cache
def _functions() -> dict[str, _Function]:
    found = {}
    for path in sorted((ROOT / "apps/workflows").glob("*.py")):
        module = import_module("apps.workflows." + path.stem)
        for name, node in _nodes(ast.parse(path.read_text()).body):
            value: object = module
            for part in name.split("."):
                value = getattr(value, part)
            assert inspect.isfunction(value), (path, name, "opaque callable")
            key = symbol(value)
            found[key] = _Function(key, module, path, node, value)
    return found


def _target(call: ast.Call, function: _Function) -> str | None:
    value = _resolve(call.func, function.module)
    return symbol(value) if inspect.isfunction(value) else None


class _Taint:
    """Actor identity data flow: locals plus summarized actor-returning helpers.

    Parameters are not tainted by callers: an identity passed into a helper as
    a value choice (for example the target owner "me") is not a relation the
    helper decides. Relations are decided where the actor is obtained.
    """

    def __init__(self) -> None:
        self.functions = _functions()
        self.sources = {symbol(require_permission), symbol(current_actor_id)}
        self.returns: dict[str, set[int] | bool] = {}
        self.local: dict[str, set[str]] = {key: set() for key in self.functions}
        changed = True
        while changed:
            changed = False
            for key, function in self.functions.items():
                changed |= self._visit(key, function)

    def call_tainted(self, call: ast.Call, function: _Function) -> bool:
        target = _target(call, function)
        return target in self.sources or self.returns.get(target or "") is True

    def tainted(self, expression: ast.AST, key: str) -> bool:
        function = self.functions[key]
        for node in ast.walk(expression):
            if isinstance(node, ast.Name) and node.id in self.local[key]:
                return True
            if isinstance(node, ast.Call) and (
                self.call_tainted(node, function)
                or isinstance(self.returns.get(_target(node, function) or ""), set)
            ):
                return True
        return False

    def identity(self, expression: ast.AST, key: str) -> bool:
        """Identity flows through values, not through ORM rows written with it."""
        if isinstance(expression, ast.Call) and any(
            isinstance(node, ast.Attribute) and node.attr == "objects"
            for node in ast.walk(expression.func)
        ):
            return False
        if isinstance(expression, ast.Name):
            return expression.id in self.local[key]
        if isinstance(expression, ast.Call) and (
            self.call_tainted(expression, self.functions[key])
            or isinstance(
                self.returns.get(_target(expression, self.functions[key]) or ""), set
            )
        ):
            return True
        return any(
            self.identity(child, key) for child in ast.iter_child_nodes(expression)
        )

    def _direct(self, value: ast.AST, key: str) -> bool:
        return (isinstance(value, ast.Name) and value.id in self.local[key]) or (
            isinstance(value, ast.Call)
            and self.call_tainted(value, self.functions[key])
        )

    def _assign(self, target: ast.AST, value: ast.AST, key: str) -> bool:
        before = set(self.local[key])
        function = self.functions[key]
        callee = _target(value, function) if isinstance(value, ast.Call) else None
        if isinstance(target, ast.Tuple) and callee in self.functions:
            positions = self.returns.get(callee or "")
            for index, element in enumerate(target.elts):
                if (
                    isinstance(positions, set)
                    and index in positions
                    and isinstance(element, ast.Name)
                ):
                    self.local[key].add(element.id)
        elif self.identity(value, key):
            # Only rebinding names carries identity; attribute/subscript stores
            # write the actor into a record and do not make the record an actor.
            elements = target.elts if isinstance(target, ast.Tuple) else [target]
            self.local[key].update(
                element.id for element in elements if isinstance(element, ast.Name)
            )
        return self.local[key] != before

    def _visit(self, key: str, function: _Function) -> bool:
        changed = False
        for _pass in range(3):
            for node in ast.walk(function.node):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        changed |= self._assign(target, node.value, key)
                elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value:
                    changed |= self._assign(node.target, node.value, key)
        for node in ast.walk(function.node):
            if isinstance(node, ast.Return) and node.value is not None:
                changed |= self._return(node.value, key)
        return changed

    def _return(self, value: ast.AST, key: str) -> bool:
        before = self.returns.get(key)
        if isinstance(value, ast.Tuple):
            positions = {
                index
                for index, element in enumerate(value.elts)
                if self._direct(element, key)
            }
            if positions and before is not True:
                self.returns[key] = (
                    before | positions if isinstance(before, set) else positions
                )
        elif self._direct(value, key):
            self.returns[key] = True
        return self.returns.get(key) != before


def _atoms(expression: ast.AST) -> Iterator[ast.AST]:
    if isinstance(expression, ast.BoolOp):
        for value in expression.values:
            yield from _atoms(value)
    elif isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        yield from _atoms(expression.operand)
    else:
        yield expression


def _roots(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.walk(node):
        if isinstance(child, ast.If | ast.While | ast.IfExp | ast.Assert):
            yield child.test
        elif isinstance(child, ast.BoolOp | ast.Compare):
            yield child


def reachable(roots: set[str]) -> set[str]:
    """Workflow functions statically callable from the given entrypoints."""
    functions = _functions()
    found = set(roots)
    pending = list(roots)
    while pending:
        function = functions.get(pending.pop())
        if function is None:
            continue
        for node in ast.walk(function.node):
            callee = _target(node, function) if isinstance(node, ast.Call) else None
            if callee in functions and callee not in found:
                found.add(callee)
                pending.append(callee)
    return found


def _relation(
    atom: ast.AST, key: str, function: _Function, taint: _Taint, predicates: set[str]
) -> bool:
    """A boolean atom decides a relation when it reads the actor identity."""
    if isinstance(atom, ast.Compare):
        return taint.tainted(atom, key)
    return (
        isinstance(atom, ast.Call)
        and (taint.tainted(atom, key) or _target(atom, function) in predicates)
        and _target(atom, function) != symbol(require_permission)
        and not taint.call_tainted(atom, function)
    )


def _relations(
    key: str, function: _Function, taint: _Taint, predicates: set[str]
) -> dict[tuple[int, int, int, int], ast.AST]:
    found = {
        _position(atom): atom
        for root in _roots(function.node)
        for atom in _atoms(root)
        if _relation(atom, key, function, taint, predicates)
    }
    found.update(
        (_position(node), node)
        for node in ast.walk(function.node)
        if isinstance(node, ast.Call) and _target(node, function) in predicates
    )
    return found


@cache
def discover_sites() -> dict[str, Site]:
    """Return every permission and actor-relation decision in apps/workflows."""
    taint = _Taint()
    functions = taint.functions
    permission = symbol(require_permission)
    predicates: set[str] = set()
    changed = True
    relations: dict[str, dict[tuple[int, int, int, int], ast.AST]] = {}
    while changed:
        relations = {
            key: _relations(key, function, taint, predicates)
            for key, function in functions.items()
        }
        found = {
            key
            for key, function in functions.items()
            for node in ast.walk(function.node)
            if isinstance(node, ast.Return)
            and node.value is not None
            and any(_position(atom) in relations[key] for atom in _atoms(node.value))
        }
        changed = found != predicates
        predicates = found
    sites: dict[str, Site] = {}
    for key, function in functions.items():
        candidates = [
            (PERMISSION, node, "")
            for node in ast.walk(function.node)
            if isinstance(node, ast.Call) and _target(node, function) == permission
        ] + [(RELATION, node, "") for node in relations[key].values()]
        candidates += _scope_sites(key, function)
        for kind, node, detail in sorted(
            candidates, key=lambda item: _position(item[1])
        ):
            base = f"{key}:{kind}:{ast.unparse(node)}"
            sid = next(
                name
                for name in (base, *(f"{base}#{n}" for n in range(2, 99)))
                if name not in sites
            )
            sites[sid] = Site(sid, key, kind, _position(node), detail)
    return sites


def _scope_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and len(node.args) == 1:
        return _scope_operand(node.args[0])
    if isinstance(node, ast.Name):
        return node.id in SCOPE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in SCOPE_NAMES
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in {"clinic", "organization"}
    )


def _local_import(func: ast.expr, function: _Function) -> object:
    """Resolve a name imported inside the function body (lazy imports)."""
    if not isinstance(func, ast.Name):
        return None
    for node in ast.walk(function.node):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) == func.id:
                    return getattr(import_module(node.module), alias.name, None)
    return None


def _manager_method(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    chain = any(
        isinstance(node, ast.Attribute) and node.attr == "objects"
        for node in ast.walk(call.func.value)
    )
    return call.func.attr if chain else None


def _scope_sites(key: str, function: _Function) -> list[tuple[str, ast.AST, str]]:
    """Record scope: a referenced or target row's clinic/org against the call.

    ORM lookups filtering on a scope column and comparisons with a scope
    operand are sites. Scope passed to a resolved callee is forwarding; a
    manager write stores scope. Anything else naming scope fails closed.
    """
    found: list[tuple[str, ast.AST, str]] = []
    for node in ast.walk(function.node):
        if isinstance(node, ast.Compare) and any(
            _scope_operand(operand) for operand in (node.left, *node.comparators)
        ):
            found.append((SCOPE, node, "compare"))
        if not isinstance(node, ast.Call):
            continue
        scoped = [
            keyword
            for keyword in node.keywords
            if keyword.arg is not None
            and (
                keyword.arg in SCOPE_NAMES
                or keyword.arg.startswith(("clinic__", "organization__"))
            )
        ]
        if not scoped:
            continue
        method = _manager_method(node)
        if method in QUERY_METHODS:
            found.extend((SCOPE, keyword, "keyword") for keyword in scoped)
        elif method in WRITE_METHODS:
            continue
        else:
            target = _resolve(node.func, function.module) or _local_import(
                node.func, function
            )
            assert method is None, (key, ast.unparse(node)[:80], "unclassified query")
            assert inspect.isfunction(target) or inspect.isclass(target), (
                key,
                ast.unparse(node)[:80],
                "unclassified record-scope use",
            )
    return found


class _Rewrite(ast.NodeTransformer):
    def __init__(
        self,
        sites: dict[tuple[int, int, int, int], Site],
        mutations: frozenset[tuple[str, str]],
    ) -> None:
        self.sites = sites
        self.mutations = mutations

    def generic_visit(self, node: ast.AST) -> ast.AST:
        visited = super().generic_visit(node)
        if isinstance(visited, ast.Call) and self.mutations:
            visited.keywords = [
                keyword
                for keyword in visited.keywords
                if not self._mutated(keyword, "remove")
            ]
            for keyword in visited.keywords:
                if self._mutated(keyword, "mismatch"):
                    keyword.value = ast.Call(ast.Name(FOREIGN, ast.Load()), [], [])
        site = self.sites.get(_position(node))
        if site is None or not isinstance(visited, ast.expr):
            return visited
        expression: ast.expr = visited
        if site.kind == RELATION:
            expression = ast.Call(
                ast.Name(RECORDER, ast.Load()),
                [ast.Constant(site.sid), expression],
                [],
            )
        mode = next((m for s, m in self.mutations if s == site.sid), None)
        if mode is not None:
            if mode == "ignore":
                expression = ast.Call(
                    ast.Name(IGNORER, ast.Load()),
                    [
                        ast.Lambda(
                            ast.arguments([], [], None, [], [], None, []), expression
                        )
                    ],
                    [],
                )
            else:
                expression = ast.BoolOp(
                    ast.Or() if mode == "true" else ast.And(),
                    [expression, ast.Constant(mode == "true")],
                )
        return ast.copy_location(expression, node)

    def _mutated(self, keyword: ast.keyword, mode: str) -> bool:
        site = self.sites.get(_position(keyword))
        return site is not None and (site.sid, mode) in self.mutations


RECORDS: list[tuple[str, bool]] = []


def _record(sid: str, value: object) -> object:
    RECORDS.append((sid, bool(value)))
    return value


def _foreign() -> UUID:
    """A scope that matches no row: the 'record scope never matches' mutant."""
    return uuid4()


def _ignore(call: Callable[[], object]) -> object:
    """Execute the permission query, then discard a refusal: 'call but ignore'."""
    try:
        return call()
    except CurrentActorError:
        return current_actor_id()


@cache
def _compiled(key: str, mutations: frozenset[tuple[str, str]]) -> CodeType:
    function = _functions()[key]
    positions = {
        site.position: site for site in discover_sites().values() if site.symbol == key
    }
    node = copy.deepcopy(function.node)
    node.decorator_list = []
    rewritten = ast.fix_missing_locations(_Rewrite(positions, mutations).visit(node))
    code = compile(
        ast.Module([rewritten], []),
        str(function.path),
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    namespace = dict(vars(function.module))
    exec(code, namespace)  # noqa: S102 - compiles audited repository source only.
    compiled = namespace[node.name]
    assert inspect.isfunction(compiled)
    return compiled.__code__


def modes(site: Site) -> tuple[str, ...]:
    """Mutation modes: discard a refusal, force a decision, or drop/miss a scope."""
    if site.kind == PERMISSION:
        return ("ignore",)
    if site.kind == SCOPE and site.detail == "keyword":
        return ("remove", "mismatch")
    return ("true", "false")


_ACTIVE: list[frozenset[tuple[str, str]]] = []


@contextmanager
def rewritten(
    mutation: tuple[str, str] | frozenset[tuple[str, str]] | None = None,
) -> Iterator[None]:
    """Record every relation decision and apply decision mutants (usually one).

    Re-entry without a mutation reuses an active rewrite, so a mutant applied
    for a whole test run also records the matrix's observations.
    """
    if _ACTIVE and mutation is None:
        yield
        return
    assert not _ACTIVE, "one rewrite at a time"
    sites = discover_sites()
    mutations = (
        frozenset()
        if mutation is None
        else mutation
        if isinstance(mutation, frozenset)
        else frozenset({mutation})
    )
    for sid, mode in mutations:
        assert sid in sites, sid
        assert mode in modes(sites[sid]), (sid, mode)
    symbols = {site.symbol for site in sites.values()}
    functions = _functions()
    originals: dict[str, CodeType] = {}
    modules = {functions[key].module for key in symbols}
    _ACTIVE.append(mutations)
    try:
        for module in modules:
            setattr(module, RECORDER, _record)
            setattr(module, IGNORER, _ignore)
            setattr(module, FOREIGN, _foreign)
        for key in sorted(symbols):
            plain = inspect.unwrap(functions[key].function)
            assert not plain.__code__.co_freevars, key
            originals[key] = plain.__code__
            plain.__code__ = _compiled(key, mutations)
        yield
    finally:
        for key, code in originals.items():
            inspect.unwrap(functions[key].function).__code__ = code
        for module in modules:
            for name in (RECORDER, IGNORER, FOREIGN):
                if hasattr(module, name):
                    delattr(module, name)
        RECORDS.clear()
        _ACTIVE.clear()


@contextmanager
def captured() -> Iterator[list[tuple[str, bool]]]:
    """Collect relation decisions evaluated inside the block."""
    assert _ACTIVE, "decision recording requires rewritten()"
    start = len(RECORDS)
    records: list[tuple[str, bool]] = []
    try:
        yield records
    finally:
        records.extend(RECORDS[start:])
